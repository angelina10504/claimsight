"""
The dashboard's look: colour tokens and the one stylesheet.

Status colours always travel with a text label (and usually an icon), never on
their own: amber and red are close for a colour-blind viewer, and a badge that
says "Awaiting human review" is readable in any light.
"""

IVORY = "#FAF8F3"
SURFACE = "#FFFFFF"
LINE = "#E6E1D6"
NAVY = "#14213D"
INK = "#1F2A44"
MUTED = "#5B6478"
TEAL = "#0F766E"
TEAL_SOFT = "#E3F1EF"
AMBER = "#B45309"
AMBER_SOFT = "#FBEFDF"
RED = "#B91C1C"
RED_SOFT = "#F9E5E5"
SLATE_SOFT = "#ECEEF2"

# tone -> (text colour, background) for badges and timeline dots
TONES = {
    "good": (TEAL, TEAL_SOFT),
    "warn": (AMBER, AMBER_SOFT),
    "bad": (RED, RED_SOFT),
    "neutral": (MUTED, SLATE_SOFT),
    "info": (NAVY, "#E4E8F1"),
}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Serif+4:opsz,wght@8..60,600&display=swap');

html, body, [class*="css"], .stApp, .stMarkdown, button, input, textarea, select {{
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
}}
.stApp {{ background: {IVORY}; color: {INK}; }}
#MainMenu, footer, [data-testid="stToolbar"] {{ visibility: hidden; }}
header[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 4.2rem; padding-bottom: 3rem; max-width: 1320px; }}

h1, h2, h3, h4 {{ color: {NAVY}; letter-spacing: -0.01em; }}
h1 {{ font-family: 'Source Serif 4', Georgia, serif; font-weight: 600; }}

section[data-testid="stSidebar"] {{ background: {NAVY}; }}
section[data-testid="stSidebar"] * {{ color: #E8ECF4; }}
/* the claim picker sits on a light field: its text must be dark */
[data-testid="stSidebar"] [data-testid="stSelectbox"] input,
[data-testid="stSidebar"] [data-testid="stSelectbox"] [role="combobox"] * {{
  color: {INK} !important; -webkit-text-fill-color: {INK} !important; }}
[data-testid="stSidebar"] [data-testid="stSelectbox"] svg {{ fill: {MUTED}; color: {MUTED}; }}
section[data-testid="stSidebar"] hr {{ border-color: rgba(255,255,255,.14); }}

.cs-brand {{ font-family: 'Source Serif 4', Georgia, serif; font-size: 1.55rem;
  font-weight: 600; color: #FFFFFF; line-height: 1.1; }}
.cs-brand-sub {{ font-size: .78rem; letter-spacing: .08em; text-transform: uppercase;
  color: #9FB3C8 !important; margin-top: .2rem; }}

.cs-eyebrow {{ font-size: .74rem; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: {TEAL}; margin-bottom: .25rem; }}
.cs-hero h1 {{ font-size: 2.35rem; margin: 0 0 .4rem 0; }}
.cs-lede {{ font-size: 1.05rem; color: {MUTED}; max-width: 60rem; line-height: 1.55; }}

.cs-card {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 14px;
  padding: 1.05rem 1.2rem; height: 100%; }}
.cs-card h4, .cs-card-title {{ margin: 0 0 .45rem 0; font-size: 1rem; font-weight: 600;
  color: {NAVY}; padding: 0; }}
.cs-card p {{ margin: 0; color: {MUTED}; font-size: .9rem; line-height: 1.5; }}
.cs-section {{ font-size: 1.15rem; font-weight: 600; color: {NAVY};
  margin: 1.6rem 0 .7rem 0; display: flex; align-items: center; gap: .55rem; }}
.cs-section .cs-tag {{ font-size: .7rem; font-weight: 600; color: {TEAL};
  background: {TEAL_SOFT}; padding: .1rem .5rem; border-radius: 999px;
  letter-spacing: .04em; text-transform: uppercase; }}

.cs-stat {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 14px;
  padding: .9rem 1.1rem; }}
.cs-stat .v {{ font-size: 1.9rem; font-weight: 700; color: {NAVY}; line-height: 1.1; }}
.cs-stat .l {{ font-size: .8rem; color: {MUTED}; margin-top: .25rem; }}
.cs-stat .k {{ font-size: .68rem; font-weight: 600; letter-spacing: .09em;
  text-transform: uppercase; color: {MUTED}; margin-bottom: .35rem; }}
.cs-stat.accent {{ border-top: 3px solid {TEAL}; }}
.cs-stat.warn {{ border-top: 3px solid {AMBER}; }}
.cs-stat.plain {{ border-top: 3px solid {LINE}; }}

.cs-badge {{ display: inline-flex; align-items: center; gap: .35rem;
  font-size: .78rem; font-weight: 600; padding: .2rem .65rem; border-radius: 999px;
  white-space: nowrap; }}
.cs-chip {{ display: inline-block; font-size: .74rem; color: {MUTED};
  background: {SLATE_SOFT}; padding: .12rem .55rem; border-radius: 6px; margin: 0 .3rem .3rem 0; }}

.cs-kv {{ display: grid; grid-template-columns: max-content 1fr; gap: .45rem 1.1rem;
  font-size: .9rem; }}
.cs-kv .k {{ color: {MUTED}; }}
.cs-kv .v {{ color: {INK}; font-weight: 500; }}

.cs-quote {{ border-left: 3px solid {TEAL}; padding: .35rem 0 .35rem .9rem;
  color: {INK}; font-size: .95rem; line-height: 1.55; background: transparent; }}

.cs-money {{ display: flex; justify-content: space-between; padding: .42rem 0;
  border-bottom: 1px dashed {LINE}; font-size: .92rem; }}
.cs-money .k {{ color: {MUTED}; }}
.cs-money.total {{ border-bottom: none; font-weight: 700; color: {NAVY}; font-size: 1.02rem;
  padding-top: .6rem; }}
.cs-payline {{ border-radius: 12px; padding: .8rem 1rem; margin-top: .6rem; }}
.cs-payline .k {{ font-size: .74rem; font-weight: 600; letter-spacing: .08em;
  text-transform: uppercase; }}
.cs-payline .v {{ font-size: 1.35rem; font-weight: 700; margin-top: .15rem; }}

.cs-table {{ width: 100%; border-collapse: separate; border-spacing: 0; font-size: .9rem;
  background: {SURFACE}; border: 1px solid {LINE}; border-radius: 14px; overflow: hidden; }}
.cs-table th {{ text-align: left; font-size: .72rem; letter-spacing: .07em;
  text-transform: uppercase; color: {MUTED}; font-weight: 600; padding: .7rem .9rem;
  background: #F4F1EA; border-bottom: 1px solid {LINE}; }}
.cs-table td {{ padding: .7rem .9rem; border-bottom: 1px solid {LINE}; vertical-align: top; }}
.cs-table tr:last-child td {{ border-bottom: none; }}
.cs-table td.num, .cs-table th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.cs-table td.muted {{ color: {MUTED}; }}

.cs-flow {{ display: flex; align-items: stretch; gap: .5rem; flex-wrap: nowrap; }}
.cs-node {{ flex: 1 1 0; background: {SURFACE}; border: 1px solid {LINE}; border-radius: 12px;
  padding: .75rem .8rem; min-width: 0; }}
.cs-node .t {{ font-weight: 600; color: {NAVY}; font-size: .92rem; }}
.cs-node .s {{ font-size: .76rem; color: {MUTED}; margin-top: .2rem; line-height: 1.4; }}
.cs-node.key {{ border-color: {TEAL}; box-shadow: inset 0 3px 0 {TEAL}; }}
.cs-arrow {{ align-self: center; color: {TEAL}; font-weight: 700; font-size: .72rem;
  text-align: center; min-width: 2.4rem; }}
.cs-arrow .line {{ font-size: 1.2rem; line-height: 1; }}
.cs-mcp {{ margin-top: .7rem; border: 1px dashed {TEAL}; border-radius: 12px;
  padding: .6rem .9rem; background: {TEAL_SOFT}; font-size: .86rem; color: {INK}; }}

.cs-tl {{ position: relative; margin-left: .55rem; border-left: 2px solid {LINE};
  padding-left: 1.2rem; }}
.cs-tl-item {{ position: relative; padding: .28rem 0; display: flex; gap: .7rem;
  align-items: baseline; }}
.cs-tl-dot {{ position: absolute; left: -1.62rem; top: .55rem; width: .72rem; height: .72rem;
  border-radius: 50%; border: 2px solid {IVORY}; }}
.cs-tl-time {{ font-size: .76rem; color: {MUTED}; font-variant-numeric: tabular-nums;
  min-width: 3.2rem; }}
.cs-tl-title {{ font-weight: 600; color: {NAVY}; font-size: .9rem; }}
.cs-tl-detail {{ font-size: .84rem; color: {MUTED}; }}

.cs-steps {{ display: flex; gap: .4rem; }}
.cs-step {{ flex: 1 1 0; border-radius: 10px; padding: .6rem .65rem; font-size: .8rem;
  border: 1px solid {LINE}; background: {SURFACE}; min-width: 0; }}
.cs-step .n {{ font-size: .68rem; font-weight: 700; letter-spacing: .08em; color: {MUTED}; }}
.cs-step .t {{ font-weight: 600; color: {NAVY}; margin-top: .15rem; line-height: 1.3; }}
.cs-step.done {{ border-color: {TEAL}; background: {TEAL_SOFT}; }}
.cs-step.now {{ border-color: {AMBER}; background: {AMBER_SOFT}; }}
.cs-step.fail {{ border-color: {RED}; background: {RED_SOFT}; }}

.cs-note {{ font-size: .82rem; color: {MUTED}; }}
.cs-banner {{ border-radius: 12px; padding: .7rem 1rem; font-size: .9rem;
  display: flex; gap: .6rem; align-items: center; margin-bottom: .9rem; }}

div[data-testid="stExpander"] {{ background: {SURFACE}; border-radius: 12px; }}
.stButton > button[kind="primary"] {{ background: {TEAL}; border-color: {TEAL}; }}
</style>
"""
