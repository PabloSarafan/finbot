import io
import re
from decimal import Decimal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np


def _strip_emoji(text: str) -> str:
    return re.sub(r'[^\w\s\(\)\-\+\.,₽%/]', '', text).strip()


def _setup_style():
    plt.rcParams.update({
        "figure.facecolor": "#1a1a2e",
        "axes.facecolor": "#16213e",
        "axes.edgecolor": "#444",
        "axes.labelcolor": "#eee",
        "text.color": "#eee",
        "xtick.color": "#aaa",
        "ytick.color": "#aaa",
        "font.family": "DejaVu Sans",
        "font.size": 11,
    })


COLORS = [
    "#e94560", "#0f3460", "#533483", "#2b9348",
    "#f4a261", "#2a9d8f", "#e9c46a", "#264653",
    "#e76f51", "#a8dadc", "#457b9d",
]


def build_pie_chart(categories: dict[str, Decimal], title: str = "Расходы по категориям") -> bytes:
    """Returns PNG bytes of a pie chart."""
    _setup_style()

    if not categories:
        return b""

    labels = [_strip_emoji(k) for k in categories.keys()]
    values = [float(v) for v in categories.values()]
    colors = COLORS[: len(labels)]

    fig, ax = plt.subplots(figsize=(8, 6))
    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct="%1.1f%%",
        colors=colors,
        startangle=90,
        pctdistance=0.82,
        wedgeprops={"linewidth": 2, "edgecolor": "#1a1a2e"},
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontsize(9)

    # Legend with amounts
    total = sum(values)
    legend_labels = [f"{l} — {v:,.0f} ₽ ({v/total*100:.0f}%)" for l, v in zip(labels, values)]
    patches = [mpatches.Patch(color=c, label=l) for c, l in zip(colors, legend_labels)]
    ax.legend(handles=patches, loc="lower center", bbox_to_anchor=(0.5, -0.25),
              ncol=2, frameon=False, fontsize=9)

    ax.set_title(title, fontsize=14, pad=20, color="#eee")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_waterfall_chart(
    income: Decimal,
    expenses_by_category: dict[str, Decimal],
    month_label: str,
) -> bytes:
    """Returns PNG bytes of a waterfall chart: income → expenses → balance."""
    _setup_style()

    labels = ["Доходы"] + [_strip_emoji(k) for k in expenses_by_category.keys()] + ["Баланс"]
    values = [float(income)] + [-float(v) for v in expenses_by_category.values()]
    balance = float(income) - sum(float(v) for v in expenses_by_category.values())
    values.append(balance)

    running = 0.0
    bottoms = []
    bar_vals = []
    bar_colors = []

    for i, v in enumerate(values):
        if i == 0:  # income bar starts at 0
            bottoms.append(0)
            bar_vals.append(v)
            bar_colors.append("#2b9348")
            running = v
        elif i == len(values) - 1:  # balance — final bar from 0
            bottoms.append(0)
            bar_vals.append(balance)
            bar_colors.append("#2b9348" if balance >= 0 else "#e94560")
        else:
            new_running = running + v
            bottoms.append(min(running, new_running))
            bar_vals.append(abs(v))
            bar_colors.append("#e94560")
            running = new_running

    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.2), 6))
    bars = ax.bar(labels, bar_vals, bottom=bottoms, color=bar_colors,
                  edgecolor="#1a1a2e", linewidth=1.5, width=0.6)

    for bar, val in zip(bars, values):
        y = bar.get_y() + bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, y + max(abs(v) for v in values) * 0.01,
                f"{val:+,.0f}", ha="center", va="bottom", fontsize=8, color="#eee")

    ax.set_title(f"Финансовый отчёт за {month_label}", fontsize=14, color="#eee")
    ax.set_ylabel("Рубли (₽)", color="#aaa")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.axhline(0, color="#aaa", linewidth=0.8, linestyle="--")
    plt.xticks(rotation=30, ha="right", fontsize=9)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
