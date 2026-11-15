import collections, datetime, json, os, sys

def when(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))

by = collections.defaultdict(list)
for name in os.listdir(sys.argv[1]):
    start = end = cmd = None
    for line in open(os.path.join(sys.argv[1], name), encoding="utf-8", errors="replace"):
        e = json.loads(line)
        if e.get("event") == "version" and start is None:
            start = e["time"]
        elif e.get("event") == "cmd_name" and cmd is None:
            cmd = e.get("name")
        elif e.get("event") == "atexit":
            end = e["time"]
    if start and end:
        by[cmd or "?"].append((when(end) - when(start)).total_seconds())
total = sum(sum(v) for v in by.values())
print(f"{sum(len(v) for v in by.values())} git processes, {total:.1f}s inside them")
for cmd, times in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:15]:
    print(f"{cmd:24} n={len(times):4} total={sum(times):6.1f}s avg={sum(times) / len(times) * 1000:6.1f}ms")
