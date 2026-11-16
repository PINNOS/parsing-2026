"""
Метрики качества и «светофор». При красном статусе — прерывание загрузки в mart.
"""


def assess(metrics: dict, thresholds: dict, lower_better: set = None):
    lower_better = lower_better or {"reject_rate", "data_age_min"}
    worst = "green"
    for k, v in metrics.items():
        g, w = thresholds.get(k, (0, 0))
        if k in lower_better:
            st = "green" if v <= g else ("yellow" if v <= w else "red")
        else:
            st = "green" if v >= g else ("yellow" if v >= w else "red")
        if st == "red":
            worst = "red"
        elif st == "yellow" and worst != "red":
            worst = "yellow"
    verdict = "NOT_OK" if worst == "red" else ("WARN" if worst == "yellow" else "OK")
    return verdict, worst
