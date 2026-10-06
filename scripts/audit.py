#!/usr/bin/env python3
"""Аудит token usage по JSONL-логам Codex-сессий.

Читает logs/*.jsonl и агрегирует usage по моделям, петлям и датам. Старый
плоский формат usage читается только для непрерывности исторических логов.

Запуск: uv run python scripts/audit.py
"""
import json
from collections import defaultdict
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"

def load_sessions() -> list[dict]:
    sessions = []
    for path in sorted(LOGS.glob("*.jsonl")):
        cur = None
        last_usage = None
        with path.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "session_start":
                    cur = ev
                    last_usage = None
                elif ev.get("event") == "token_usage" and cur:
                    last_usage = ev
                elif ev.get("event") == "result" and cur:
                    u = ev.get("usage") or {}
                    total = u.get("total") or {}
                    in_tokens = total.get("inputTokens", u.get("input_tokens", 0))
                    out_tokens = total.get("outputTokens", u.get("output_tokens", 0))
                    cached_tokens = total.get(
                        "cachedInputTokens", u.get("cache_read_input_tokens", 0)
                    )
                    observed_total = (last_usage or {}).get("total_tokens")
                    sessions.append({
                        "date": ev.get("ts", "")[:10],
                        # логи до 2026-08-09 писались без имени тьютора — тогда он был один
                        "tutor": ev.get("tutor") or cur.get("tutor") or "interview",
                        "loop": ev.get("loop") or cur.get("loop"),
                        "model": cur.get("model"),
                        "dry_run": cur.get("dry_run", False),
                        "in": in_tokens,
                        "out": out_tokens,
                        "cached": cached_tokens,
                        "total": total.get(
                            "totalTokens",
                            observed_total if observed_total is not None
                            else in_tokens + out_tokens,
                        ),
                        "steps": (last_usage or {}).get(
                            "model_steps", ev.get("num_turns", 0)
                        ),
                    })
                    cur = None
                    last_usage = None
    return sessions


def show(title: str, rows: list[dict], key) -> None:
    g = defaultdict(lambda: defaultdict(float))
    for s in rows:
        k = key(s)
        g[k]["n"] += 1
        for f in ("in", "out", "cached", "total", "steps"):
            g[k][f] += s[f]
    print(f"\n=== {title} ===")
    print(f"{'key':<22}{'n':>5}{'input':>13}{'cached':>13}{'output':>13}"
          f"{'total':>13}{'steps':>8}")
    tot = defaultdict(float)
    for k, v in sorted(g.items(), key=lambda kv: -kv[1]["total"]):
        print(f"{str(k):<22}{int(v['n']):>5}{int(v['in']):>13,}"
              f"{int(v['cached']):>13,}{int(v['out']):>13,}"
              f"{int(v['total']):>13,}{int(v['steps']):>8,}")
        for f in v:
            tot[f] += v[f]
    print(f"{'TOTAL':<22}{int(tot['n']):>5}{int(tot['in']):>13,}"
          f"{int(tot['cached']):>13,}{int(tot['out']):>13,}"
          f"{int(tot['total']):>13,}{int(tot['steps']):>8,}")


def main() -> None:
    sessions = load_sessions()
    if not sessions:
        print("Сессий нет")
        return
    live = [s for s in sessions if not s["dry_run"]]
    print(f"Сессий: {len(sessions)} (боевых {len(live)}), "
          f"период {min(s['date'] for s in sessions)}..{max(s['date'] for s in sessions)}")

    show("По модели", live, lambda s: s["model"])
    show("По тьютору", live, lambda s: s["tutor"])
    show("По петле", live, lambda s: f"{s['tutor']}/{s['loop']}")
    show("По дате (тренд — главное, за чем следить)", live, lambda s: s["date"])

    # Токены на model step — ранний индикатор распухания product context.
    g = defaultdict(lambda: [0, 0])
    for s in live:
        key = f"{s['tutor']}/{s['loop']}"
        g[key][0] += s["total"]
        g[key][1] += s["steps"]
    print("\n=== Средние токены на model step ===")
    for k, (tokens, steps) in sorted(g.items(), key=lambda kv: -kv[1][0]):
        if steps:
            print(f"{str(k):<24}{tokens / steps:>10,.0f} ток/step")


if __name__ == "__main__":
    main()
