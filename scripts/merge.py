#!/usr/bin/env python3
"""Merge GKD data without evaluating it as code. Run from any directory."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import json5

ROOT = Path(__file__).resolve().parents[1]
META = {"name", "desc", "snapshotUrls", "excludeSnapshotUrls", "exampleUrls"}
LINKS = {"preKeys", "actionCdKey", "actionMaximumKey"}
ARRAYS = {"matches", "anyMatches", "excludeMatches", "activityIds",
          "excludeActivityIds", "versionNames", "excludeVersionNames",
          "versionCodes", "excludeVersionCodes", "preKeys", "scopeKeys"}
COMMON = {"actionCd", "actionDelay", "fastQuery", "matchRoot", "matchDelay",
          "matchTime", "actionMaximum", "resetMatch", "actionCdKey",
          "actionMaximumKey", "order", "forcedTime", "priorityTime",
          "priorityActionMaximum", "activityIds", "excludeActivityIds",
          "versionNames", "excludeVersionNames", "versionCodes",
          "excludeVersionCodes", "versionCode", "versionName"}
DEFAULTS = {"actionCd": 1000, "fastQuery": False, "matchRoot": False,
            "resetMatch": "activity", "order": 0, "priorityActionMaximum": 1}
KNOWN_CATEGORIES = ["开屏广告", "青少年模式", "更新提示", "评价提示", "通知提示",
                    "权限提示", "局部广告", "全屏广告", "分段广告", "功能类", "其他"]
MAX_BYTES = 32 * 1024 * 1024


def array(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canon(value).encode()).hexdigest()


def clean(obj, omit=()):
    """Only discard documentation fields at the current object level."""
    return {k: array(v) if k in ARRAYS else v for k, v in obj.items()
            if k not in META and k not in omit and v is not None}


def rules(group):
    return [({"matches": r} if isinstance(r, str) else copy.deepcopy(r))
            for r in array(group["rules"])]


def mapped_key(namespace, key):
    # Stable across upstream insertions and edits; never use content hashes as IDs.
    if type(key) is not int or not -500_000 <= key < 500_000:
        raise ValueError(f"group key outside supported namespace: {key!r}")
    zigzag = 2 * key if key >= 0 else -2 * key - 1
    result = namespace * 1_000_000 + zigzag
    if not 0 <= result < 2**31:
        raise ValueError("GKD group key overflow")
    return result


def all_containers(sub):
    yield "@global", sub.get("globalGroups", [])
    for app in sub.get("apps", []):
        yield app["id"], app["groups"]


def validate(sub, expected_id=None):
    if not isinstance(sub, dict) or not isinstance(sub.get("name"), str):
        raise ValueError("not a GKD subscription")
    for k in ("id", "version"):
        if type(sub.get(k)) is not int or not 0 <= sub[k] < 2**31:
            raise ValueError(f"invalid subscription {k}")
    if expected_id is not None and sub["id"] != expected_id:
        raise ValueError(f"unexpected source ID {sub['id']}, expected {expected_id}")
    apps = sub.get("apps", [])
    if len({a["id"] for a in apps}) != len(apps):
        raise ValueError("duplicate app IDs")
    cats = sub.get("categories", [])
    if len({c["key"] for c in cats}) != len(cats):
        raise ValueError("duplicate category keys")
    count = 0
    for appid, groups in all_containers(sub):
        keys = [g["key"] for g in groups]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate group keys in {appid}")
        for g in groups:
            if type(g["key"]) is not int or not isinstance(g.get("name"), str):
                raise ValueError("invalid group identity")
            for target in array(g.get("scopeKeys")):
                if target not in keys:
                    raise ValueError(f"dangling scopeKeys: {appid}/{g['key']} -> {target}")
            rr = rules(g)
            rkeys = [r["key"] for r in rr if r.get("key") is not None]
            if len(rkeys) != len(set(rkeys)):
                raise ValueError(f"duplicate rule keys: {appid}/{g['key']}")
            for r in rr:
                # Upstream uses disabled empty rules as informational shell-app entries.
                if not any(r.get(k) for k in ("matches", "anyMatches")) and not (r == {} and g.get("enable") is False):
                    raise ValueError(f"rule without selectors: {appid}/{g['key']}")
                for k in ("matches", "anyMatches", "excludeMatches"):
                    if any(not isinstance(s, str) or not s for s in array(r.get(k))):
                        raise ValueError(f"invalid {k}")
            count += len(rr)
    if count == 0:
        raise ValueError("empty subscription")
    # Reject NaN/Infinity and any value not representable in strict JSON.
    canon(sub)
    return count


def category_names(sub, group):
    return [c["name"] for c in sub.get("categories", [])
            if group["name"].startswith(c["name"])]


def prepare(sub, appid, group):
    g = copy.deepcopy(group)
    g["rules"] = rules(g)
    if g.get("disableIfAppGroupMatch") == "":
        g["disableIfAppGroupMatch"] = g["name"]
    g["enable"] = g.get("enable", True)
    if appid != "@global":
        for category in sub.get("categories", []):
            if g["name"].startswith(category["name"]):
                if category.get("enable") is not None:
                    g["enable"] = category["enable"]
                break
    return g


def components(groups):
    """Undirected scope graph: incoming references protect their target too."""
    positions = {g["key"]: i for i, g in enumerate(groups)}
    edges = {i: set() for i in range(len(groups))}
    for i, g in enumerate(groups):
        for key in array(g.get("scopeKeys")):
            j = positions[key]
            edges[i].add(j)
            edges[j].add(i)
    seen = set()
    for i in range(len(groups)):
        if i in seen:
            continue
        pending, found = [i], set()
        while pending:
            j = pending.pop()
            if j in found:
                continue
            found.add(j)
            pending.extend(edges[j] - found)
        seen.update(found)
        yield sorted(found)


def context(sub, appid, group, prefixes):
    if appid == "@global":
        return {"enable": group["enable"]}
    names = category_names(sub, group)
    return {"categories": names, "enable": group["enable"],
            "uncategorizedName": group["name"] if not names else None,
            "globalPrefixes": [p for p in prefixes if group["name"].startswith(p)],
            "ignoreGlobalGroupMatch": group.get("ignoreGlobalGroupMatch", False)}


def component_signature(sub, appid, groups, prefixes):
    """Normalize keys to positions without changing actual output rule keys."""
    indexes = {g["key"]: i for i, g in enumerate(groups)}
    rindexes = [{r["key"]: i for i, r in enumerate(g["rules"])
                 if r.get("key") is not None} for g in groups]

    def resolve(gi, key):
        for gj in [gi] + [indexes[k] for k in array(groups[gi].get("scopeKeys"))]:
            if key in rindexes[gj]:
                return [gj, rindexes[gj][key]]
        # Preserve upstream's unresolved references without accidentally resolving them.
        return ["unresolved", key]

    result = []
    for gi, group in enumerate(groups):
        row = clean(group, {"key", "rules", "scopeKeys"})
        row["context"] = context(sub, appid, group, prefixes)
        row["scopeKeys"] = [indexes[k] for k in array(group.get("scopeKeys"))]
        for k in LINKS:
            if k in row:
                row[k] = [resolve(gi, x) for x in array(row[k])]
        row["rules"] = []
        for r in group["rules"]:
            rule = clean(r, {"key"})
            for k in LINKS:
                if k in rule:
                    rule[k] = [resolve(gi, x) for x in array(rule[k])]
            row["rules"].append(rule)
        result.append(row)
    return digest(result)


def independent(group):
    return not group.get("scopeKeys") and not any(
        obj.get(k) is not None for obj in [group] + group["rules"] for k in LINKS
    )


def rule_signature(sub, appid, group, rule, prefixes):
    effective = dict(DEFAULTS)
    effective.update(clean({k: v for k, v in group.items() if k in COMMON}))
    effective.update(clean(rule, {"key"}))
    effective.setdefault("action", "clickCenter" if rule.get("position") is not None
                         else "swipe" if rule.get("swipeArg") is not None else "click")
    for k in ARRAYS:
        if k in effective:
            effective[k] = array(effective[k])
    extra = clean(group, COMMON | {"key", "rules", "enable", "scopeKeys",
                                  "ignoreGlobalGroupMatch"})
    return digest({"rule": effective, "group": extra,
                   "context": context(sub, appid, group, prefixes)})


def merge(sources, config):
    """sources is an ordered list of (configuration, parsed subscription)."""
    prefixes = sorted({g.get("disableIfAppGroupMatch") or g["name"]
                       for _, sub in sources for g in sub.get("globalGroups", [])
                       if g.get("disableIfAppGroupMatch") is not None})
    categories = {}
    apps, global_groups = {}, []
    component_seen, rule_seen = {}, {}
    removed, provenance = [], []
    input_groups = input_rules = 0
    for source, sub in sources:
        validate(sub, source["id"])
        for c in sub.get("categories", []):
            categories.setdefault(c["name"], {"name": c["name"]})
        for app in sub.get("apps", []):
            apps.setdefault(app["id"], {"id": app["id"], "name": app.get("name", app["id"]), "groups": []})
        for appid, raw_groups in all_containers(sub):
            groups = [prepare(sub, appid, g) for g in raw_groups]
            output = global_groups if appid == "@global" else apps[appid]["groups"]
            input_groups += len(groups)
            input_rules += sum(len(g["rules"]) for g in groups)
            retained = []
            for indexes in components(groups):
                block = [groups[i] for i in indexes]
                identity = {"source": source["key"], "app": appid,
                            "groups": [g["key"] for g in block]}
                fingerprint = (appid, component_signature(sub, appid, block, prefixes))
                if fingerprint in component_seen:
                    removed.append({**identity, "reason": "equivalent-component",
                                    "rules": sum(len(g["rules"]) for g in block),
                                    "kept": component_seen[fingerprint]})
                    continue
                component_seen[fingerprint] = identity
                # Never split multi-group dependencies or stateful rule sequences.
                if len(block) == 1 and block[0]["rules"] and independent(block[0]):
                    g = block[0]
                    keep = []
                    for ri, rule in enumerate(g["rules"]):
                        sig = (appid, rule_signature(sub, appid, g, rule, prefixes))
                        rid = {**identity, "ruleIndex": ri, "ruleKey": rule.get("key")}
                        if sig in rule_seen:
                            removed.append({**rid, "reason": "equivalent-independent-rule",
                                            "rules": 1, "kept": rule_seen[sig]})
                        else:
                            rule_seen[sig] = rid
                            keep.append(rule)
                    g["rules"] = keep
                    if not keep:
                        continue
                for i, g in zip(indexes, block):
                    retained.append((i, g))
            for _, g in sorted(retained):
                old_key = g["key"]
                g["key"] = mapped_key(source["namespace"], old_key)
                if "scopeKeys" in g:
                    g["scopeKeys"] = [mapped_key(source["namespace"], k)
                                      for k in array(g["scopeKeys"])]
                output.append(g)
                provenance.append({"app": appid, "key": g["key"], "source": source["key"],
                                   "originalKey": old_key, "name": g["name"], "rules": len(g["rules"])})
    for name, category in categories.items():
        category["key"] = (KNOWN_CATEGORIES.index(name) if name in KNOWN_CATEGORIES
                           else 1000 + int(hashlib.sha256(name.encode()).hexdigest()[:7], 16))
    out = {"id": config["id"], "name": config["name"], "version": 1,
           "author": "ltt2077 / 原规则作者见仓库来源说明",
           "checkUpdateUrl": "./gkd.version.json5",
           "supportUri": f"https://github.com/{config['repository']}",
           "categories": sorted(categories.values(), key=lambda c: c["key"]),
           "globalGroups": global_groups,
           "apps": [a for a in sorted(apps.values(), key=lambda a: a["id"]) if a["groups"]]}
    output_rules = validate(out)
    output_groups = len(global_groups) + sum(len(a["groups"]) for a in out["apps"])
    report = {"inputGroups": input_groups, "inputRules": input_rules,
              "outputApps": len(out["apps"]), "outputGroups": output_groups,
              "outputRules": output_rules, "removedRules": input_rules - output_rules,
              "removedGroups": input_groups - output_groups,
              "duplicates": removed, "provenance": provenance}
    assert sum(r["rules"] for r in removed) == report["removedRules"]
    return out, report


def parse(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json5.loads(text, allow_duplicate_keys=False)


def read_source(source, root, offline=False):
    cache = root / "sources/cache" / (source["key"] + ".json")
    if "path" in source:
        data = parse((root / source["path"]).read_text(encoding="utf-8"))
        validate(data, source["id"])
        return data, {"status": "local-fixed"}, None
    if offline:
        data = json.loads(cache.read_text(encoding="utf-8"))
        validate(data, source["id"])
        return data, {"status": "cached-offline"}, None
    error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(source["url"], headers={"User-Agent": "GKD-Merged/1.0"})
            with urllib.request.urlopen(req, timeout=45) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("source exceeds size limit")
            data = parse(raw.decode("utf-8-sig"))
            new_count = validate(data, source["id"])
            if cache.exists():
                previous = json.loads(cache.read_text(encoding="utf-8"))
                if data["version"] < previous["version"]:
                    raise ValueError("upstream version rollback")
                if new_count < validate(previous) * 0.5:
                    raise ValueError("source lost over 50% of rules; manual review required")
            return data, {"status": "fresh"}, data
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(2 ** attempt)
    if not cache.exists():
        raise RuntimeError(f"{source['key']}: {error}; no last-good cache")
    data = json.loads(cache.read_text(encoding="utf-8"))
    validate(data, source["id"])
    print(f"::warning::{source['key']} unavailable; keeping last-good cache", file=sys.stderr)
    return data, {"status": "stale-cache", "error": error}, None


def write_json(path, value, pretty=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
               if pretty else canon(value)) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def publish(out, report, root, checked_at):
    target = root / "dist/gkd.json5"
    previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
    body = {k: v for k, v in out.items() if k != "version"}
    changed = previous is None or body != {k: v for k, v in previous.items() if k != "version"}
    if previous is not None and previous["id"] != out["id"]:
        raise ValueError("refusing to change subscription ID")
    out["version"] = 1 if previous is None else previous["version"] + int(changed)
    validate(out)
    report.update({"checkedAt": checked_at, "version": out["version"],
                   "contentChanged": changed, "contentSha256": digest(body)})
    write_json(target, out)
    write_json(root / "dist/gkd.version.json5", {"id": out["id"], "version": out["version"]})
    write_json(root / "dist/report.json", report)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="build using last-good snapshots")
    args = parser.parse_args()
    config = json.loads((ROOT / "sources/config.json").read_text())
    namespaces = [s["namespace"] for s in config["sources"]]
    if len(set(namespaces)) != len(namespaces):
        raise ValueError("duplicate source namespaces")
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(lambda s: read_source(s, ROOT, args.offline), config["sources"]))
    sources = [(s, f[0]) for s, f in zip(config["sources"], fetched)]
    out, report = merge(sources, config)
    report["sources"] = [{"key": s["key"], "location": s.get("url", s.get("path")),
                          "id": d["id"], "version": d["version"], "name": d["name"],
                          "rules": validate(d), "sha256": digest(d), **f[1]}
                         for (s, d), f in zip(sources, fetched)]
    changed = publish(out, report, ROOT, datetime.now(timezone.utc).isoformat())
    # Never replace snapshots before the entire merged output has passed validation.
    for source, result in zip(config["sources"], fetched):
        if result[2] is not None:
            write_json(ROOT / "sources/cache" / (source["key"] + ".json"), result[2], pretty=False)
    summary = {k: report[k] for k in ("version", "outputApps", "outputGroups", "outputRules", "removedRules")}
    print(json.dumps({**summary, "changed": changed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
