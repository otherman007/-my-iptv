import json, re, time, urllib.request, urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

CFG = json.loads(Path("config.json").read_text(encoding="utf-8"))
OUT = Path("output.m3u")
REPORT = Path("report.json")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"

def fetch(url, timeout=10, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit) if limit else r.read()

def decode_bytes(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")

def parse_m3u(text, source, priority, skip_probe=False):
    lines = [x.strip() for x in text.replace("\r","").split("\n")]
    result, info = [], None
    for line in lines:
        if line.startswith("#EXTINF:"):
            info = line
        elif line and not line.startswith("#") and info:
            name = info.split(",",1)[1].strip() if "," in info else "未命名"
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', info))
            result.append({
                "name": name,
                "url": line,
                "logo": attrs.get("tvg-logo",""),
                "tvg_id": attrs.get("tvg-id",""),
                "orig_group": attrs.get("group-title",""),
                "source": source,
                "priority": priority,
                "skip_probe": skip_probe,
            })
            info = None
    return result

def normalize_name(name):
    s = name.strip()
    aliases = CFG.get("aliases", {})
    if s in aliases:
        s = aliases[s]
    key = re.sub(r'[\s_\-—·（）()\[\]【】]+', '', s).upper()
    key = key.replace("高清","").replace("超清","").replace("HD","").replace("4K","")
    return key, s

def classify(ch):
    hay = f'{ch["name"]} {ch.get("orig_group","")}'.upper()
    cats = CFG["categories"]
    # 央视频道（包括 CCTV-5）统一归到“央视”；其余体育频道仍优先识别。
    for kw in cats.get("央视", []):
        if kw.upper() in hay:
            return "央视"
    order = ["体育","卫视","港澳台","影视"]
    for cat in order:
        for kw in cats.get(cat, []):
            if kw.upper() in hay:
                return cat
    return None

def probe(ch):
    if ch.get("skip_probe"):
        return 0
    url = ch["url"]
    if not url.startswith(("http://","https://")):
        return None
    start = time.monotonic()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Range": "bytes=0-2047"
            }
        )
        with urllib.request.urlopen(req, timeout=CFG.get("timeout_seconds",7)) as r:
            code = getattr(r, "status", 200)
            ctype = (r.headers.get("Content-Type") or "").lower()
            sample = r.read(2048)
        latency = round((time.monotonic()-start)*1000)
        looks_media = (
            "mpegurl" in ctype or "video/" in ctype or
            b"#EXTM3U" in sample or
            url.lower().split("?")[0].endswith((".m3u8",".ts",".mpd"))
        )
        if 200 <= code < 400 and looks_media:
            return latency
    except Exception:
        return None
    return None

def main():
    allch = []
    source_stats = {}
    sources = CFG.get("sources", []) + CFG.get("custom_sources", [])
    for src in sources:
        if not src.get("enabled") or not src.get("url"):
            continue
        try:
            text = decode_bytes(fetch(src["url"], timeout=12))
            entries = parse_m3u(
                text, src["name"], src.get("priority",50), src.get("skip_probe",False)
            )
            source_stats[src["name"]] = {"parsed": len(entries), "error": ""}
            allch.extend(entries)
            print(f'[OK] {src["name"]}: {len(entries)}')
        except Exception as e:
            source_stats[src["name"]] = {"parsed": 0, "error": str(e)}
            print(f'[FAIL] {src["name"]}: {e}')

    # 手工动态频道位：只使用用户主动填入的地址
    for ch in CFG.get("dynamic_channels", []):
        if ch.get("enabled") and ch.get("url"):
            allch.append({
                "name": ch["name"], "url": ch["url"], "logo": "",
                "tvg_id": "", "orig_group": ch.get("group","体育"),
                "source": "dynamic", "priority": 0,
                "skip_probe": ch.get("skip_probe",False)
            })

    filtered = []
    for ch in allch:
        cat = classify(ch)
        if cat:
            ch["group"] = cat
            ch["norm"], ch["display_name"] = normalize_name(ch["name"])
            filtered.append(ch)

    # 去重 URL
    uniq, seen = [], set()
    for ch in filtered:
        k = (ch["norm"], ch["url"])
        if k not in seen:
            seen.add(k)
            uniq.append(ch)

    # 并发测速
    latencies = {}
    with ThreadPoolExecutor(max_workers=18) as ex:
        futs = {ex.submit(probe, ch): i for i, ch in enumerate(uniq)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                latencies[i] = fut.result()
            except Exception:
                latencies[i] = None

    valid = []
    for i, ch in enumerate(uniq):
        if latencies.get(i) is not None:
            ch["latency_ms"] = latencies[i]
            valid.append(ch)

    # 同频道多源择优：先 priority，再 latency；最多保留 N 个备用
    buckets = defaultdict(list)
    for ch in valid:
        buckets[(ch["group"], ch["norm"])].append(ch)

    selected = []
    maxn = max(1, int(CFG.get("max_sources_per_channel",2)))
    for _, arr in buckets.items():
        arr.sort(key=lambda x: (x["priority"], x["latency_ms"]))
        for n, ch in enumerate(arr[:maxn], 1):
            ch["backup_index"] = n
            selected.append(ch)

    group_order = {"央视":0,"卫视":1,"港澳台":2,"体育":3,"影视":4}
    selected.sort(key=lambda x: (
        group_order.get(x["group"],99), x["display_name"], x["backup_index"]
    ))

    epg = CFG.get("epg","")
    header = '#EXTM3U'
    if epg:
        header += f' x-tvg-url="{epg}"'
    lines = [header]

    for ch in selected:
        name = ch["display_name"]
        if ch["backup_index"] > 1:
            name = f'{name} · 备用{ch["backup_index"]}'
        attrs = [
            f'group-title="{ch["group"]}"',
            f'x-latency="{ch["latency_ms"]}ms"'
        ]
        if ch.get("tvg_id"):
            attrs.append(f'tvg-id="{ch["tvg_id"]}"')
        if ch.get("logo"):
            attrs.append(f'tvg-logo="{ch["logo"]}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')
        lines.append(ch["url"])

    OUT.write_text("\n".join(lines)+"\n", encoding="utf-8")

    counts = defaultdict(int)
    for ch in selected:
        counts[ch["group"]] += 1
    report = {
        "generated_channels": len(selected),
        "counts": dict(counts),
        "source_stats": source_stats,
        "note": "测速发生在 GitHub Actions 机房，与你家宽带的实际延迟可能不同。"
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
