"""
Small HTML pieces for the dashboard. Every value that came from a claim is
escaped: a narrative is text a claimant wrote, and must render as text.
"""

from html import escape

import streamlit as st

from styles import TONES


def e(value) -> str:
    return escape("" if value is None else str(value))


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def rupees(value) -> str:
    return "—" if value is None else f"₹{float(value):,.2f}"


def badge(label: str, tone: str = "neutral", icon: str = "") -> str:
    fg, bg = TONES.get(tone, TONES["neutral"])
    mark = f"<span aria-hidden='true'>{e(icon)}</span>" if icon else ""
    return f"<span class='cs-badge' style='color:{fg};background:{bg}'>{mark}{e(label)}</span>"


def section(title: str, tag: str = "") -> None:
    html(f"<div class='cs-section'>{e(title)}"
         + (f"<span class='cs-tag'>{e(tag)}</span>" if tag else "") + "</div>")


def stat(value, label: str, kicker: str = "", style: str = "plain") -> str:
    return (f"<div class='cs-stat {style}'>"
            + (f"<div class='k'>{e(kicker)}</div>" if kicker else "")
            + f"<div class='v'>{e(value)}</div><div class='l'>{e(label)}</div></div>")


def stat_row(items: list) -> None:
    cols = st.columns(len(items))
    for col, item in zip(cols, items):
        with col:
            html(stat(*item))


def card(title: str, body: str, raw_body: bool = False) -> str:
    return (f"<div class='cs-card'><div class='cs-card-title'>{e(title)}</div>"
            f"<p>{body if raw_body else e(body)}</p></div>")


def kv(pairs: list) -> str:
    rows = "".join(f"<div class='k'>{e(k)}</div><div class='v'>{v if raw else e(v)}</div>"
                   for k, v, raw in ((p + (False,))[:3] for p in pairs))
    return f"<div class='cs-kv'>{rows}</div>"


def table(headers: list, rows: list, numeric: tuple = ()) -> str:
    """rows hold cells already escaped or built from badge()."""
    head = "".join(f"<th class='{'num' if i in numeric else ''}'>{e(h)}</th>"
                   for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(
        f"<td class='{'num' if i in numeric else ''}'>{cell}</td>" for i, cell in enumerate(row))
        + "</tr>" for row in rows)
    return f"<table class='cs-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def money_row(label: str, value: str, total: bool = False) -> str:
    return (f"<div class='cs-money{' total' if total else ''}'><span class='k'>{e(label)}</span>"
            f"<span>{e(value)}</span></div>")


def payline(label: str, value: str, tone: str) -> str:
    fg, bg = TONES.get(tone, TONES["neutral"])
    return (f"<div class='cs-payline' style='background:{bg};color:{fg}'>"
            f"<div class='k'>{e(label)}</div><div class='v'>{e(value)}</div></div>")


def banner(text: str, tone: str = "info", icon: str = "") -> None:
    fg, bg = TONES.get(tone, TONES["info"])
    html(f"<div class='cs-banner' style='background:{bg};color:{fg}'>"
         f"<strong aria-hidden='true'>{e(icon)}</strong><span>{text}</span></div>")


def timeline_item(at, title: str, detail: str, tone: str) -> str:
    fg, _ = TONES.get(tone, TONES["neutral"])
    when = "" if at is None else f"{float(at):.1f}s"
    return (f"<div class='cs-tl-item'><span class='cs-tl-dot' style='background:{fg}'></span>"
            f"<div class='cs-tl-time'>{e(when)}</div><div><span class='cs-tl-title'>{e(title)}</span>"
            + (f"<span class='cs-tl-detail'> · {e(detail)}</span>" if detail else "")
            + "</div></div>")


def steps(items: list) -> None:
    """items: (title, state) with state done | now | fail | todo."""
    cells = "".join(f"<div class='cs-step {state}'><div class='n'>STEP {i}</div>"
                    f"<div class='t'>{e(title)}</div></div>"
                    for i, (title, state) in enumerate(items, start=1))
    html(f"<div class='cs-steps'>{cells}</div>")
