from __future__ import annotations

import json
import csv
import hashlib
import io
import html
import re
import sys
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
UI_DIR = Path(__file__).resolve().parent
for _p in (str(SRC), str(UI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import streamlit as st
import streamlit.components.v1 as components_v1

from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.llm.factory import provider_from_environment
from legal_funds_agent.domain.models import Claim, DecisionType, ReviewDecision, TransactionReviewAction
from legal_funds_agent.services.candidate_matcher import (
    candidate_review_priority,
    candidate_risk_level,
    match_claim_transactions,
    sort_candidates_for_review,
)
from legal_funds_agent.services.case_report_service import case_report_to_json
from legal_funds_agent.services.evidence_conflict_service import (
    build_evidence_conflict_matrix,
    showcase_conflict_matrix,
)
from legal_funds_agent.services.report_service import build_report, report_to_csv, report_to_html, report_to_json
from legal_funds_agent.parsers.file_parsers import extract_document_text, extract_transactions_csv_detailed
from legal_funds_agent.services.review_engine import build_decision
from legal_funds_agent.services.statement_extractor import StatementPaymentFact
from legal_funds_agent.services.verification_engine import find_duplicate_transactions
from legal_funds_agent.services.transaction_analysis import (
    identify_refund_transactions,
    transaction_canonical_key,
    unique_transactions,
)
from legal_funds_agent.workflow.vertical_slice import (
    WorkflowResult,
    confirm_claim_extraction,
    review_transactions,
    run_case_inputs,
    run_demo_case,
)


st.set_page_config(page_title="资金链证审", page_icon=None, layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
:root {
    --paper: #f8fafc;
    --surface: #ffffff;
    --ink: #020617;
    --muted: #475569;
    --line: #e2e8f0;
    --navy: #0f172a;
    --navy-hover: #1e293b;
    --gold: #b28a48;
    --focus-ring: #0f172a;
    --sidebar-bg: #f8fafc;
    --status-ok: #276749;
    --status-ok-bg: #eaf3ee;
    --status-partial: #9a6700;
    --status-partial-bg: #fdf6e6;
    --status-conflict: #dc2626;
    --status-conflict-bg: #fef2f2;
    --status-insufficient: #475569;
    --status-insufficient-bg: #f1f5f9;
    --status-excluded: #475467;
    --status-excluded-bg: #eceef1;
}

/* Global resets & Typography */
html {
    font-size: 16.5px;
}
/* 数据工作台密度分级：caption 与辅助说明比正文更紧凑 */
[data-testid="stCaptionContainer"], .stCaption {
    font-size: 13.5px !important;
}
.stApp {
    background: var(--paper);
    color: var(--ink);
}
html, body, [class*="css"] {
    font-family: Inter, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

section.main > div {
    max-width: 1360px;
    margin: 0 auto;
    padding: 2rem clamp(1.25rem, 4vw, 4.5rem) 4rem;
}

/* Hide default streamlit clutter */
[data-testid="stAppDeployButton"], [data-testid="stMainMenuButton"] {
    display: none !important;
}
/* Hide the auto anchor-link icon (🔗) that Streamlit 1.62 adds next to headings */
[data-testid="stHeaderActionElements"] {
    display: none !important;
}
[data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapseButton"] button,
[data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapsedControl"] button {
    visibility: visible !important;
    opacity: 1 !important;
}

/* Swiss Sidebar: Neutral #fafaf8 with 1px border */
.stApp [data-testid="stSidebar"] {
    background: var(--sidebar-bg);
    border-right: 1px solid var(--line);
    box-shadow: none;
}
[data-testid="stSidebar"] > div:first-child {
    padding: 1.5rem 1.1rem 2.5rem;
}
[data-testid="stSidebar"] * {
    color: var(--ink);
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    color: var(--muted);
}
.sidebar-brand-block {
    padding: 0 0 16px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 20px;
}
.sidebar-brand-badge {
    display: inline-block;
    width: 8px;
    height: 8px;
    background: var(--gold);
    margin-right: 8px;
    vertical-align: 2px;
}
.sidebar-brand-name {
    font-size: 17.5px;
    font-weight: 750;
    letter-spacing: 0.04em;
    color: var(--ink);
}
.sidebar-brand-sub {
    font-size: 12px;
    letter-spacing: 0.14em;
    color: var(--muted);
    text-transform: uppercase;
    margin-top: 4px;
    padding-left: 16px;
    font-weight: 600;
}

/* Sidebar Navigation Radio (pure link style with 2px navy bar) */
[data-testid="stSidebar"] .stRadio > label {
    display: none !important;
}
[data-testid="stSidebar"] .stRadio div[role="radiogroup"] {
    gap: 3px;
}
[data-testid="stSidebar"] .stRadio label {
    padding: 10px 14px;
    border-left: 2px solid transparent;
    border-radius: 0px !important;
    font-size: 17px;
    font-weight: 550;
    min-height: 44px;
    display: flex;
    align-items: center;
    transition: background 0.15s ease, border-left-color 0.15s ease;
    cursor: pointer;
}
[data-testid="stSidebar"] .stRadio label:hover {
    background: #f0f2f4;
}
[data-testid="stSidebar"] .stRadio label:has(input:checked) {
    background: #eef1f5;
    border-left-color: var(--navy);
    font-weight: 700;
    color: var(--navy);
}
[data-testid="stSidebar"] .stRadio label > div:first-child {
    display: none;
}
[data-testid="stSidebar"] .stRadio div[data-testid="stMarkdownContainer"] p {
    margin: 0;
    font-size: 16.5px;
    letter-spacing: 0.02em;
}
/* The navigation is rendered as buttons for reliable jumps, but keeps the
   former radio/link visual language: flat white surface, left alignment and
   navy active marker instead of a dark filled primary button. */
[data-testid="stSidebar"] .stButton button[aria-label*="案件审查概览"],
[data-testid="stSidebar"] .stButton button[aria-label*="涉案资金流水"],
[data-testid="stSidebar"] .stButton button[aria-label*="资金证据核验"],
[data-testid="stSidebar"] .stButton button[aria-label*="审查底稿留痕"],
[data-testid="stSidebar"] .stButton button[aria-label*="案件关系图"] {
    min-height: 44px !important;
    justify-content: flex-start !important;
    text-align: left !important;
    border: 0 !important;
    border-left: 2px solid transparent !important;
    border-radius: 0 !important;
    background: transparent !important;
    color: var(--ink) !important;
    font-size: 16.5px !important;
    font-weight: 550 !important;
    padding-left: 14px !important;
}
[data-testid="stSidebar"] .stButton button[aria-label*="案件审查概览"]:hover,
[data-testid="stSidebar"] .stButton button[aria-label*="涉案资金流水"]:hover,
[data-testid="stSidebar"] .stButton button[aria-label*="资金证据核验"]:hover,
[data-testid="stSidebar"] .stButton button[aria-label*="审查底稿留痕"]:hover,
[data-testid="stSidebar"] .stButton button[aria-label*="案件关系图"]:hover {
    background: #f0f2f4 !important;
    border-left-color: #bcc3cd !important;
}
/* Selected nav item is rendered as a primary button, then flattened back to
   the link style with the navy left bar as the single active marker. */
[data-testid="stSidebar"] .stButton button[aria-label*="案件审查概览"][kind="primary"],
[data-testid="stSidebar"] .stButton button[aria-label*="涉案资金流水"][kind="primary"],
[data-testid="stSidebar"] .stButton button[aria-label*="资金证据核验"][kind="primary"],
[data-testid="stSidebar"] .stButton button[aria-label*="审查底稿留痕"][kind="primary"],
[data-testid="stSidebar"] .stButton button[aria-label*="案件关系图"][kind="primary"] {
    background: #eef1f5 !important;
    border-left-color: var(--navy) !important;
    color: var(--navy) !important;
    font-weight: 700 !important;
}

/* Swiss Editorial Masthead */
.case-masthead {
    border-bottom: 1px solid var(--line);
    padding: 8px 0 24px;
    margin: 0 auto 26px;
    max-width: 1440px;
}
.masthead-top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 14.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
}
.masthead-case-code {
    font-family: Inter, monospace;
    font-weight: 650;
    color: var(--ink);
    letter-spacing: 0.08em;
}
.masthead-status-badge {
    font-size: 14.5px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 0px;
    border: 1px solid var(--line);
    background: var(--surface);
}
.case-masthead h1 {
    font-size: 40px;
    line-height: 1.15;
    margin: 16px 0 6px;
    color: var(--ink);
    font-weight: 750;
    letter-spacing: -0.02em;
}
/* 首页品牌标题大于案件页标题，避免每个案件页都像宣传页 */
.case-masthead.landing-masthead h1 {
    font-size: 46px;
}
.masthead-subtitle {
    font-size: 17.5px;
    font-weight: 500;
    margin: 0 0 12px;
    color: var(--navy);
    letter-spacing: 0.01em;
}
.masthead-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--muted);
    font-size: 15px;
    letter-spacing: 0.02em;
}
.masthead-meta .meta-dot {
    color: var(--gold);
    font-weight: 700;
    padding: 0 3px;
}

/* Swiss Stat Strip: Pure 4-column Grid */
.stat-strip {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    margin: 18px 0 30px;
    background: transparent;
}
.stat-item {
    min-height: 106px;
    padding: 16px 20px 14px 0;
    border-right: 1px solid var(--line);
}
.stat-item:not(:first-child) {
    padding-left: 20px;
}
.stat-item:last-child {
    border-right: 0;
}
.stat-label {
    font-size: 13.5px;
    letter-spacing: 0.08em;
    color: var(--muted);
    text-transform: uppercase;
    font-weight: 600;
}
.stat-value {
    color: var(--ink);
    font-size: 31px;
    line-height: 1.15;
    font-weight: 650;
    margin: 10px 0 6px;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
}
.stat-note {
    color: var(--muted);
    font-size: 14.5px;
}

/* Section Headings */
.section-heading {
    display: flex;
    align-items: baseline;
    gap: 12px;
    border-bottom: 1px solid var(--line);
    padding: 12px 0 10px;
    margin: 26px 0 16px;
}
.section-heading .section-kicker {
    font-size: 13.5px;
    letter-spacing: 0.1em;
    color: var(--gold);
    font-weight: 700;
    text-transform: uppercase;
}
.section-heading strong {
    color: var(--ink);
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.01em;
}
.section-heading .heading-sub {
    color: var(--muted);
    font-size: 16px;
    margin-left: auto;
}
.section-kicker {
    font-size: 13.5px;
    letter-spacing: 0.1em;
    color: var(--gold);
    font-weight: 700;
    text-transform: uppercase;
    margin: 14px 0 8px;
}

/* Accessible Status: Symbol + Text + Color */
.accessible-status {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 15px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 0px;
    line-height: 1.3;
}
.accessible-status .status-symbol {
    font-weight: 700;
    font-size: 16px;
}
.accessible-status.status-ok {
    color: var(--status-ok);
    background: var(--status-ok-bg);
    border: 1px solid rgba(39, 103, 73, 0.3);
}
.accessible-status.status-partial {
    color: var(--status-partial);
    background: var(--status-partial-bg);
    border: 1px solid rgba(154, 103, 0, 0.3);
}
.accessible-status.status-conflict {
    color: var(--status-conflict);
    background: var(--status-conflict-bg);
    border: 1px solid rgba(162, 59, 50, 0.3);
}
.accessible-status.status-insufficient {
    color: var(--status-insufficient);
    background: var(--status-insufficient-bg);
    border: 1px solid rgba(102, 112, 133, 0.3);
}
.accessible-status.status-excluded {
    color: var(--status-excluded);
    background: var(--status-excluded-bg);
    border: 1px solid rgba(71, 84, 103, 0.3);
}
.accessible-status.status-pending {
    color: var(--navy);
    background: #eef2f6;
    border: 1px solid rgba(24, 50, 74, 0.25);
}

/* Original Evidence Quote in Serif */
.source-quote {
    border-left: 3px solid var(--gold);
    margin: 16px 0 20px;
    padding: 12px 18px;
    background: #fafaf8;
    border-radius: 0;
}
.source-quote .source-kicker {
    font-size: 13.5px;
    letter-spacing: 0.08em;
    color: var(--muted);
    text-transform: uppercase;
    font-weight: 600;
    margin-bottom: 6px;
}
.source-quote blockquote {
    font-family: "Source Han Serif SC", "Noto Serif SC", "Songti SC", "SimSun", serif;
    font-size: 16.5px;
    line-height: 1.75;
    margin: 6px 0 8px;
    color: #212529;
}
.source-quote figcaption {
    color: var(--muted);
    font-size: 13.5px;
    font-family: Inter, monospace;
}

/* Review Step Navigation Bar */
.step-nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    padding: 12px 20px;
    margin: 18px 0 24px;
    background: var(--surface);
}
.step-nav-item {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 16px;
    font-weight: 600;
    color: var(--muted);
}
.step-nav-item .step-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: 1px solid var(--line);
    font-size: 13.5px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    background: #fafaf8;
}
.step-nav-item.active {
    color: var(--navy);
    font-weight: 700;
}
.step-nav-item.active .step-num {
    background: var(--navy);
    color: #fff;
    border-color: var(--navy);
}
.step-nav-item.done {
    color: var(--status-ok);
    font-weight: 650;
}
.step-nav-item.done .step-num {
    background: var(--status-ok-bg);
    color: var(--status-ok);
    border-color: rgba(39, 103, 73, 0.4);
}
.step-nav-divider {
    color: #b8c1cc;
    font-size: 14.5px;
    user-select: none;
}

/* Editorial Claim Box */
.claim-editorial-box {
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    padding: 16px 0 18px;
    margin: 12px 0 20px;
}
.claim-editorial-kicker {
    font-size: 13.5px;
    letter-spacing: 0.1em;
    color: var(--muted);
    text-transform: uppercase;
    font-weight: 600;
    margin-bottom: 6px;
}
.claim-editorial-amount {
    font-size: 35px;
    font-weight: 700;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    margin-bottom: 8px;
    letter-spacing: -0.02em;
}
.claim-editorial-desc {
    font-size: 17px;
    line-height: 1.6;
    color: #334155;
    margin: 0;
}

/* Review Items List (Editorial Fact Rows) */
.claim-items-list {
    border-top: 1px solid var(--line);
    margin: 16px 0 24px;
}
.claim-item-row {
    border-bottom: 1px solid var(--line);
    padding: 14px 0 16px;
}
.claim-item-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
}
.claim-item-code {
    font-family: Inter, monospace;
    font-size: 13.5px;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-weight: 600;
    text-transform: uppercase;
}
.claim-item-title {
    font-size: 16.5px;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 4px;
    line-height: 1.4;
}
.claim-item-amount {
    font-size: 22px;
    font-weight: 700;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
}

/* Review Issues (Evidence Conflict Matrix) */
.review-issue {
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    padding: 18px 0;
    margin: 16px 0 24px;
}
.issue-kicker {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13.5px;
    letter-spacing: 0.08em;
    color: var(--muted);
    text-transform: uppercase;
    font-weight: 600;
    margin-bottom: 8px;
}
.review-issue h3 {
    margin: 6px 0 14px;
    color: var(--ink);
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.01em;
}
.issue-materials {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 16px;
    padding: 12px 0;
    border-top: 1px solid #edf0f3;
    border-bottom: 1px solid #edf0f3;
}
.material-col small {
    display: block;
    color: var(--muted);
    font-size: 13.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
    margin-bottom: 4px;
}
.material-quote {
    font-family: "Source Han Serif SC", "Noto Serif SC", "Songti SC", "SimSun", serif;
    font-size: 16.5px;
    line-height: 1.6;
    margin: 0;
    color: #212529;
}
.issue-conclusion, .issue-next {
    padding-top: 12px;
    font-size: 16px;
}
.issue-conclusion b, .issue-next b {
    color: var(--muted);
    font-size: 13.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    display: block;
    margin-bottom: 2px;
}
.issue-conclusion p, .issue-next p {
    margin: 0;
    line-height: 1.5;
}
.issue-next p {
    color: var(--navy);
    font-weight: 600;
}

/* High Risk Transaction Panel */
.risk-panel {
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    border-left: 3px solid var(--status-conflict);
    padding: 16px 18px;
    margin: 16px 0 22px;
    background: #ffffff;
}
.risk-kicker {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13.5px;
    letter-spacing: 0.08em;
    color: var(--muted);
    text-transform: uppercase;
    font-weight: 600;
}
.risk-panel h3 {
    margin: 8px 0 12px;
    color: var(--ink);
    font-size: 20px;
    font-weight: 700;
}
.risk-panel h3 .risk-amount {
    float: right;
    color: var(--navy);
    font-size: 22px;
    font-variant-numeric: tabular-nums;
}
.risk-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px 18px;
    border-top: 1px solid #edf0f3;
    padding: 12px 0 8px;
    font-size: 16px;
}
.risk-grid small {
    display: block;
    color: var(--muted);
    font-size: 13.5px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-bottom: 2px;
}
.risk-reason {
    color: var(--status-conflict);
    font-size: 16px;
    margin: 8px 0 0;
    line-height: 1.5;
}

/* Review Summary Memo */
.review-summary {
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    background: #ffffff;
    padding: 20px 22px;
    margin: 16px 0 24px;
}
.review-summary h2 {
    margin: 8px 0 10px;
    font-size: 24px;
    font-weight: 700;
}
.review-summary p {
    max-width: 840px;
    color: #475467;
    line-height: 1.65;
    margin: 0;
    font-size: 17px;
}

/* Evidence Card */
.evidence-card {
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    background: transparent;
    padding: 16px 0;
    margin: 14px 0 20px;
}
.evidence-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}
.evidence-header h4 {
    color: var(--navy);
    margin: 0;
    font-size: 17.5px;
    font-weight: 750;
}
.evidence-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px 20px;
}
.evidence-cell {
    font-size: 16.5px;
}
.evidence-label {
    color: var(--muted);
    font-size: 14px;
    display: block;
    margin-bottom: 4px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.evidence-value {
    color: var(--ink);
    font-size: 16.5px;
    font-weight: 650;
    overflow-wrap: anywhere;
    font-variant-numeric: tabular-nums;
}

/* Accessible Notice */
.accessible-notice {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 14px;
    font-size: 16px;
    line-height: 1.5;
    margin: 10px 0 14px;
    border-radius: 0px;
}
.accessible-notice .notice-icon {
    font-weight: 700;
    font-size: 17px;
    line-height: 1.3;
}
.accessible-notice.ok {
    background: var(--status-ok-bg);
    border-left: 3px solid var(--status-ok);
    color: var(--status-ok);
}
.accessible-notice.warn {
    background: var(--status-partial-bg);
    border-left: 3px solid var(--status-partial);
    color: #784c00;
}
.accessible-notice.danger {
    background: var(--status-conflict-bg);
    border-left: 3px solid var(--status-conflict);
    color: var(--status-conflict);
}

/* Legal Notice */
.legal-notice {
    border-left: 3px solid var(--navy);
    background: #f4f6f8;
    color: #334155;
    padding: 12px 16px;
    font-size: 16px;
    line-height: 1.6;
    margin: 20px 0;
    border-radius: 0;
}

/* Topology Diagram Container */
.topology-shell {
    background: #ffffff;
    border: 1px solid var(--line);
    border-radius: 0px;
    padding: 16px;
    margin: 12px 0 20px;
    overflow: auto;
}
.topology-shell pre {
    margin: 0;
    font-size: 14.5px;
    line-height: 1.4;
    font-family: Inter, monospace;
}

/* Streamlit Native Controls Override */
div[data-testid="stDataFrame"] {
    border: 1px solid var(--line) !important;
    border-radius: 0px !important;
    overflow: hidden;
    background: var(--surface) !important;
}
div[data-testid="stExpander"] {
    border: 1px solid var(--line) !important;
    border-radius: 0px !important;
    background: transparent !important;
}
.stButton > button, .stDownloadButton > button {
    border-radius: 2px !important;
    min-height: 44px !important;
    font-weight: 650 !important;
    letter-spacing: 0.02em !important;
    cursor: pointer !important;
}
/* Streamlit 原生上传组件的英文提示本地化：隐藏英文说明，用中文伪元素替代 */
[data-testid="stFileUploaderDropzoneInstructions"] span,
[data-testid="stFileUploaderDropzoneInstructions"] small {
    display: none !important;
}
[data-testid="stFileUploaderDropzoneInstructions"]::after {
    content: "将文件拖拽到此处";
    font-size: 14.5px;
    color: var(--muted);
}
[data-testid="stFileUploaderDropzone"] button {
    font-size: 0 !important;
}
[data-testid="stFileUploaderDropzone"] button::after {
    content: "选择文件";
    font-size: 15px;
}
.stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
    background: var(--navy) !important;
    border-color: var(--navy) !important;
    color: #ffffff !important;
}
.stButton > button[kind="primary"]:hover, .stDownloadButton > button[kind="primary"]:hover {
    background: var(--navy-hover) !important;
    border-color: var(--navy-hover) !important;
}
.stButton > button[kind="secondary"], .stDownloadButton > button[kind="secondary"] {
    background: #ffffff !important;
    border-color: var(--line) !important;
    color: var(--ink) !important;
}
.stButton > button[kind="secondary"]:hover, .stDownloadButton > button[kind="secondary"]:hover {
    background: #f4f6f8 !important;
    border-color: #bcc3cd !important;
}
[data-testid="stFileUploader"] {
    border: 1px dashed #9aa6b2 !important;
    border-radius: 0px !important;
    background: var(--surface) !important;
}
/* Compact dropzone: uploader shrinks to an entry-style control once files are registered */
[data-testid="stFileUploaderDropzone"] {
    min-height: 0 !important;
    padding: 10px 14px !important;
}
[data-testid="stFileUploaderDropzoneInstructions"] {
    font-size: 14.5px !important;
}
[data-testid="stTabs"] [data-baseweb="tab-list"] {
    gap: 22px;
    border-bottom: 1px solid var(--line);
    background: transparent;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
    padding: 10px 4px;
    font-size: 17px;
    font-weight: 550;
    color: var(--muted);
    border-radius: 0px !important;
    border-bottom: 2px solid transparent !important;
    background: transparent !important;
}
[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {
    color: var(--navy) !important;
    font-weight: 700 !important;
    border-bottom-color: var(--navy) !important;
}

/* Accessible focus ring */
*:focus-visible {
    outline: 3px solid var(--focus-ring) !important;
    outline-offset: 2px !important;
}
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        transition-duration: 0.01ms !important;
    }
}
@media (max-width: 760px) {
    .stat-strip, .issue-materials, .risk-grid, .evidence-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .stat-item:nth-child(2) {
        border-right: 0;
    }
    .stat-item:nth-child(n+3) {
        border-top: 1px solid var(--line);
    }
}
@media (max-width: 480px) {
    .evidence-grid, .risk-grid, .issue-materials {
        grid-template-columns: 1fr;
    }
    section.main > div {
        padding-top: 1.2rem;
    }
}
</style>
""", unsafe_allow_html=True)

# 拦截 Streamlit 前端把裸 c/r 键绑成快捷键的行为：焦点在页面空白处按 Ctrl+C 复制时
# 会误触发“Clear caches”对话框。组件 iframe sandbox 含 allow-same-origin，可安全访问父窗口。
components_v1.html("""
<script>
(function(){
  try {
    var w = window.parent;
    if (!w || w.__kcInterceptorInstalled) return;
    w.__kcInterceptorInstalled = true;
    w.addEventListener('keydown', function(e){
      if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C' || e.key === 'r' || e.key === 'R')) {
        e.stopImmediatePropagation();
      }
    }, true);
  } catch (err) { /* cross-origin sandbox: give up silently */ }
})();
</script>
""", height=0)


def _mask(value: str | None) -> str:
    if not value:
        return "-"
    return "*" * max(len(value) - 4, 0) + value[-4:]


def _load_case_display_names() -> dict[str, str]:
    """Display aliases for saved cases; the case id itself is immutable."""
    database_path = ROOT / "data" / "cases.db"
    if not database_path.exists():
        return {}
    with closing(connect(database_path)) as connection:
        return Repository(connection).load_case_display_names()


def _load_demo(provider):
    return run_demo_case(ROOT / "sample_data" / "demo_case_001", provider=provider)


def _persist_result(result, *, audit_events=None) -> Path:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    database_path = data_dir / "cases.db"
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        repository.save_transactions(list(result.transactions.values()))
        claims_to_save = result.claims if getattr(result, "claims", None) else [result.claim]
        for c in claims_to_save:
            if c and c.extraction_status == "human_confirmed":
                repository.save_claim(c)
        # The immutable claims table only holds signed (human_confirmed) claims.
        # Keep the full extraction set in the mutable snapshot so that a restore
        # does not silently drop claims the reviewer has not confirmed yet.
        repository.save_case_snapshot(result.claim.case_id, claims_to_save)
        # Only the model-produced baseline belongs in the initial checkpoint.
        # Human decisions are immutable signed records and are saved separately
        # after the review has passed verification.
        system_decisions = {
            d.id: d
            for d in getattr(result, "system_decisions_by_claim", {}).values()
            if d and d.decision_type == DecisionType.SYSTEM_PROPOSED
        }
        if result.system_decision and result.system_decision.decision_type == DecisionType.SYSTEM_PROPOSED:
            system_decisions[result.system_decision.id] = result.system_decision
        for d in system_decisions.values():
            repository.save_decision(d)
        repository.save_audit_events(result.audit_events if audit_events is None else audit_events)
    return database_path


def _supplementary_documents(result) -> list[dict[str, str]]:
    """Return registered supplements, restoring public Gold Case supplements when possible."""
    documents = st.session_state.get("supplementary_documents", [])
    if documents or result.claim.case_id != "GOLD_CASE_001":
        return documents
    package_dir = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_001" / "visible" / "documents"
    restored: list[dict[str, str]] = []
    for filename in ("03_证人证言.docx", "04_被告人供述与辩解.docx"):
        path = package_dir / filename
        if path.exists():
            restored.append({"filename": filename, "text": extract_document_text(path.read_bytes(), filename=filename)})
    return restored


def _json_arg(payload) -> str:
    """Deterministic JSON passed to the cached model calls.

    The serialized content *is* the cache key: Streamlit hashes the argument, so identical
    content hits the cache and the model is not re-billed on every rerender. Passing a
    pre-computed hash here instead would be a bug — the cached function has to parse the
    argument back into data.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


@st.cache_data(show_spinner=False)
def _cached_conflict_enrichment(entries_json: str, facts_json: str, materials_json: str,
                                provider_name: str) -> list[dict]:
    """Model enrichment for the conflict matrix, cached on content.

    Streamlit rerenders on every interaction, so calling the model inline would bill the
    same case repeatedly. The key is the content itself, not the render.
    """
    from legal_funds_agent.services.evidence_conflict_service import enrich_conflict_entries

    entries = json.loads(entries_json)
    try:
        provider = provider_from_environment(provider_name)
    except Exception:
        return entries
    return enrich_conflict_entries(
        entries, json.loads(facts_json), json.loads(materials_json), provider
    )


@st.cache_data(show_spinner=False)
def _cached_checklist_notes(payload_json: str, provider_name: str) -> dict:
    """Model rewording of the checklist. ``status`` is deliberately not part of the key.

    Returns both the wording and the audit of how it was produced, so a failed or
    rejected rewrite is surfaced in the UI instead of degrading silently.
    """
    from legal_funds_agent.services.case_report_service import request_investigation_notes

    try:
        provider = provider_from_environment(provider_name)
    except Exception as exc:
        return {"notes": {}, "report": {"checked": False, "notes": [f"PROVIDER_UNAVAILABLE:{type(exc).__name__}"]}}
    notes, report = request_investigation_notes(json.loads(payload_json), provider)
    return {"notes": notes, "report": report}


@st.cache_data(show_spinner=False)
def _cached_narrative(facts_json: str, provider_name: str) -> dict:
    """Model narrative over the deterministic fact pack, cached on the fact pack.

    Returns the narrative (possibly ``None``) plus the audit explaining why, so the page
    can tell the operator that the summary was not produced rather than just omitting it.
    """
    from legal_funds_agent.services.case_narrative_service import generate_narrative_from_facts

    try:
        provider = provider_from_environment(provider_name)
    except Exception as exc:
        return {"narrative": None, "audit": {"checked": False, "notes": [f"PROVIDER_UNAVAILABLE:{type(exc).__name__}"]}}
    narrative, audit = generate_narrative_from_facts(json.loads(facts_json), provider)
    return {"narrative": narrative, "audit": audit}


def _conflict_matrix_for(result, supplementary_documents: list[dict[str, str]]) -> list[dict]:
    """Pick the conflict matrix implementation for the case at hand.

    GOLD_CASE_001 is the fabricated showcase, and its four conflicts are fixed
    presentation content. Every other case goes through the generic path, which derives
    third-party account facts from the transactions instead of returning nothing.
    """
    if getattr(result.claim, "case_id", "") == "GOLD_CASE_001":
        return showcase_conflict_matrix(result.transactions, supplementary_documents)
    from legal_funds_agent.services.evidence_conflict_service import deterministic_conflict_entries

    claims = result.claims if getattr(result, "claims", None) else [result.claim]
    entries, facts = deterministic_conflict_entries(result.transactions, claims)
    if not entries:
        return []
    materials = [
        {"label": str(document.get("filename") or f"材料{index}"), "text": str(document.get("text") or "")}
        for index, document in enumerate(supplementary_documents or [], start=1)
        if str(document.get("text") or "").strip()
    ]
    if not materials or not _model_enhancement_enabled():
        return entries
    return _cached_conflict_enrichment(
        _json_arg(entries), _json_arg(facts), _json_arg(materials), provider_name
    )


def _apply_model_wording(checklist: list[dict], provider_name: str) -> list[dict]:
    """Reword the checklist via the model, keeping the deterministic facts authoritative.

    Numbers are constrained to each item's own facts, so a rejected rewrite simply keeps
    the template text. The outcome is reported on the page rather than degrading silently.
    """
    from legal_funds_agent.services.case_report_service import apply_investigation_notes

    payload = [
        {
            "item_id": item["item_id"],
            "category": item.get("category"),
            "priority": item.get("priority"),
            "target": item.get("target"),
            "facts": item.get("facts") or {},
            "current_suggestion": item.get("suggestion"),
            "current_next_action": item.get("next_action"),
        }
        for item in checklist
    ]
    if not payload:
        return checklist
    outcome = _cached_checklist_notes(_json_arg(payload), provider_name)
    notes = outcome.get("notes") or {}
    report = outcome.get("report") or {}
    if notes:
        st.caption(f"模型增强：回查建议措辞已改写 {len(notes)} 项。")
        return apply_investigation_notes(checklist, notes)
    reason = report.get("rejected") or "；".join(report.get("notes") or []) or "模型未返回改写"
    st.caption(f"模型增强：回查建议措辞未改写（{reason}）。已保留确定性模板文字。")
    return checklist


def _model_enhancement_enabled() -> bool:
    return bool(st.session_state.get("model_enhancement_enabled", False))


PROVIDER_CHOICES = ["mock", "deepseek"]

# Truthy spellings accepted by the ``?enhance=`` URL override.
TRUTHY_QUERY_VALUES = {"1", "true", "yes"}


def _provider_label(value: str) -> str:
    return "本地 Mock（推荐演示）" if value == "mock" else "DeepSeek API"


def _render_fund_flow(graph, *, height: int = 430, key: str | None = None, transactions=None, disputed_names=None) -> None:
    """Render the fund flow topology with the Cytoscape.js + ELK component."""
    if not graph.nodes or not graph.edges:
        st.markdown(
            '<div class="topology-shell"><div class="section-kicker">账户关系总览 · 拓扑结构</div>'
            '<pre>暂无资金流向数据</pre></div>',
            unsafe_allow_html=True,
        )
        return

    from components.fund_flow import render_fund_flow, topology_to_payload

    payload = topology_to_payload(graph, transactions=transactions, disputed_names=disputed_names)
    # The component keeps its own selection across reruns, so the echo is only
    # a rebuild aid: when Streamlit does remount the subtree (tab switch,
    # changed data) the highlight is re-applied from this value. It must NOT
    # trigger a second rerun on every tap — the script re-execution is what
    # made the page flash and dropped the graph back to a refitted view.
    sel_state_key = f"{key}__selection_echo"
    last_selection = st.session_state.get(sel_state_key)
    if last_selection:
        payload["selected"] = last_selection
    state = render_fund_flow(payload, height=height, key=key)

    selection = None
    if state is not None:
        selection = getattr(state, "selection", None)
        if selection is None and isinstance(state, dict):
            selection = state.get("selection")
    current_selection = (
        {"type": selection.get("type"), "id": selection.get("id")} if selection else None
    )
    if current_selection != last_selection:
        st.session_state[sel_state_key] = current_selection
    if not selection:
        st.caption("点击图中账户或资金连线可查看来源明细；滚轮缩放、拖拽平移。")
        return

    if selection.get("type") == "node":
        role_cn = {
            "victim": "被害人 / 资金来源",
            "suspect": "涉案一级账户",
            "third_party_disputed": "! 第三方争议账户",
            "downstream": "后续流向账户",
        }.get(selection.get("display_role"), "其他账户")
        _evidence_card(
            f"账户 · {selection.get('name', '-')}",
            [
                ("账户性质", role_cn),
                ("脱敏账号", selection.get("masked_account") or "-"),
                ("累计流入", selection.get("total_in_full", "-")),
                ("累计流出", selection.get("total_out_full", "-")),
            ],
        )
    elif selection.get("type") == "edge":
        date_min, date_max = selection.get("date_min", ""), selection.get("date_max", "")
        dates = date_min if date_min == date_max else f"{date_min} ~ {date_max}"
        reason = selection.get("reason") or ""
        source_refs = selection.get("source_refs") or []
        items = [
            ("交易笔数", f"{selection.get('count', 0)} 笔"),
            ("处置状态", selection.get("disposition_label", "-")),
            ("日期范围", dates or "-"),
            ("说明", REASON_TO_CN.get(reason, reason) or "-"),
        ]
        for idx, ref in enumerate(source_refs[:8], 1):
            locator_parts = [p for p in (ref.get("evidence_id"), ref.get("account_id")) if p]
            locator = " / ".join(locator_parts)
            row = ref.get("source_row")
            if row:
                locator = f"{locator} · 第{row}行" if locator else f"第{row}行"
            items.append((f"来源记录 {idx:02d}", f"{ref.get('transaction_id', '-')}" + (f" · {locator}" if locator else "")))
        if len(source_refs) > 8:
            items.append(("…", f"另有 {len(source_refs) - 8} 笔来源记录"))
        _evidence_card(
            f"资金往来 · {selection.get('amount_full', '-')}",
            items,
        )


def _save_confirmed_claim(database_path: Path, claim) -> None:
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        repository.save_claim(claim)
        # Keep the mutable snapshot in sync so a later restore sees the
        # confirmed status instead of the pre-confirmation extraction.
        snapshot = repository.load_case_snapshot(claim.case_id)
        if snapshot is not None:
            updated = [claim if c.id == claim.id else c for c in snapshot]
            if all(c.id != claim.id for c in snapshot):
                updated.append(claim)
            repository.save_case_snapshot(claim.case_id, updated)


def _decision_core_payload(decision: ReviewDecision) -> dict:
    payload = decision.model_dump(mode="json")
    return {
        key: payload.get(key)
        for key in (
            "case_id", "claim_id", "decision_type",
            "status", "included_transaction_ids", "excluded_transaction_ids",
            "disputed_transaction_ids", "covered_amount", "uncovered_amount",
            "disputed_amount", "reason_codes", "verification_error_codes",
            "transaction_review_actions", "reviewer", "note",
        )
    }


def _save_human_review(database_path: Path, decision, audit_events) -> ReviewDecision:
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        # A fresh browser session can rebuild the same signed review from the
        # system baseline (v1), even when the database already contains v2/v3.
        # Reuse the latest matching human record instead of attempting to
        # insert an older immutable version again.
        existing_decisions = repository.list_decisions(decision.claim_id)
        latest_human = next(
            (
                item for item in reversed(existing_decisions)
                if item.decision_type == DecisionType.HUMAN_CONFIRMED
            ),
            None,
        )
        if latest_human and _decision_core_payload(latest_human) == _decision_core_payload(decision):
            return latest_human
        try:
            repository.save_decision(decision)
        except ValueError as exc:
            # Replaying the same signed version is a valid idempotent retry.
            # A different payload with the same immutable ID must still fail.
            if not str(exc).startswith("immutable decision already exists:"):
                raise
            row = connection.execute(
                "SELECT payload_json FROM decisions WHERE id = ?", (decision.id,)
            ).fetchone()
            stored = ReviewDecision.model_validate_json(row["payload_json"]) if row else None
            if stored is None or _decision_core_payload(stored) != _decision_core_payload(decision):
                raise
            return stored
        repository.save_audit_events(audit_events)
    return decision


def _load_investigation_items(database_path: Path | None, case_id: str) -> list[dict]:
    if not database_path:
        return []
    with closing(connect(database_path)) as connection:
        return Repository(connection).load_investigation_items(case_id)


def _save_investigation_items(database_path: Path | None, case_id: str, items: list[dict]) -> None:
    if not database_path:
        return
    with closing(connect(database_path)) as connection:
        Repository(connection).save_investigation_items(case_id, items)


def _next_available_case_id(database_path: Path, base: str = "CASE-0001") -> str:
    """Suggest the next unused case id when the default already has signed claims."""
    if not database_path.exists():
        return base
    with closing(connect(database_path)) as connection:
        repo = Repository(connection)
        cases = repo.list_cases()
    existing = {c["case_id"] for c in cases}
    if base not in existing:
        return base
    # Case ids are not guaranteed to be hyphenated — the evaluation package uses
    # GOLD_CASE_001 — so never assume a "-<number>" suffix is present.
    if "-" in base:
        prefix, num_part = base.rsplit("-", 1)
        try:
            start = int(num_part)
        except ValueError:
            prefix, start = base, 0
    else:
        prefix, start = base, 0
    for n in range(start + 1, start + 10000):
        candidate = f"{prefix}-{n:04d}"
        if candidate not in existing:
            return candidate
    return f"{prefix}-{start + 1:04d}"


def _restore_case_from_database(database_path: Path, case_id: str):
    """Rebuild the in-memory workflow view from the immutable local snapshot."""
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        # The mutable extraction snapshot preserves every extracted claim
        # (including unconfirmed ones); the immutable claims table only holds
        # signed claims. Prefer the snapshot so multi-claim cases restore whole.
        claims = repository.load_case_snapshot(case_id) or repository.load_case_claims(case_id)
        transactions = repository.load_case_transactions(case_id)
        decisions = repository.load_latest_decisions_by_claim(case_id)
        audit_events = repository.load_case_audit_events(case_id)

    if not claims and not transactions:
        return None

    first_claim = claims[0] if claims else None
    if first_claim is None:
        # Orphaned transactions are not a valid claim or a signed review snapshot.
        return None

    from dataclasses import replace
    from legal_funds_agent.services.candidate_matcher import find_weak_payer_signals
    from legal_funds_agent.services.transaction_analysis import normalize_party_name

    candidates_by_claim = {
        claim.id: match_claim_transactions(claim, list(transactions.values()))
        for claim in claims
    }
    # Rebuild read-only recall signals after refresh; never replace signed decisions.
    victims = frozenset(normalize_party_name(claim.victim_name) for claim in claims)
    weak_signals_by_claim = {
        claim.id: find_weak_payer_signals(claim, list(transactions.values()), exclude_payer_names=victims)
        for claim in claims
    }
    key_owners = {}
    for claim_id, candidates in candidates_by_claim.items():
        for candidate in candidates:
            event_key = transaction_canonical_key(transactions[candidate.transaction_id])
            key_owners.setdefault(event_key, set()).add(claim_id)
    for claim_id, candidates in candidates_by_claim.items():
        candidates_by_claim[claim_id] = [
            replace(candidate, risk_codes=tuple(dict.fromkeys((*candidate.risk_codes, "CROSS_CLAIM_DUPLICATION"))))
            if len(key_owners[transaction_canonical_key(transactions[candidate.transaction_id])]) > 1
            else candidate
            for candidate in candidates
        ]
    first_candidates = candidates_by_claim.get(first_claim.id, [])
    first_decision = decisions.get(first_claim.id)
    if first_decision is None:
        first_decision = build_decision(
            first_claim, transactions, has_pending_candidates=bool(first_candidates),
        )
        decisions[first_claim.id] = first_decision

    duplicate_groups = find_duplicate_transactions(list(transactions.values()))
    # Snapshots do not preserve statement extraction. Do not fabricate corroboration
    # by copying the indictment's amount into an invented statement fact.
    statement_fact = None
    result = WorkflowResult(
        task_id=f"RESTORED-{case_id}",
        claim=first_claim,
        claim_locators=[
            locator
            for claim in claims
            for locator in getattr(claim, "source_locators", [])
        ],
        statement_fact=statement_fact,
        statement_conflicts=[],
        duplicate_groups=duplicate_groups,
        transactions=transactions,
        candidates=first_candidates,
        system_decision=first_decision,
        audit_events=audit_events,
        claims=claims,
        candidates_by_claim=candidates_by_claim,
        system_decisions_by_claim=decisions,
        weak_signals_by_claim=weak_signals_by_claim,
        statement_extraction_warnings=["RESTORED_STATEMENT_NOT_AVAILABLE"],
        statement_facts_by_victim={claim.victim_name: None for claim in claims},
    )
    human_decision = (
        first_decision if first_decision.decision_type.value == "HUMAN_CONFIRMED" else None
    )
    report = build_report(
        first_claim, human_decision, transactions,
        claim_locators=[], statement_conflicts=[], duplicate_groups=duplicate_groups,
    ) if human_decision else None
    return result, human_decision, report


def _restore_case_into_session(database_path: Path, case_id: str) -> bool:
    restored = _restore_case_from_database(database_path, case_id)
    if restored is None:
        return False
    result, decision, report = restored
    st.session_state.result = result
    st.session_state.repository_path = database_path
    if decision is not None:
        st.session_state.decision = decision
        st.session_state.report = report
    else:
        st.session_state.pop("decision", None)
        st.session_state.pop("report", None)
    st.session_state.pop("failed_audit_events", None)
    st.session_state.pop("supplementary_documents", None)
    for k in [k for k in st.session_state if str(k).endswith("__selection_echo")]:
        st.session_state.pop(k, None)
    return True


def render_case_masthead(
    case_id: str,
    *,
    status: str = "◌ 等待人工复核",
    case_name: str | None = None,
    case_type: str = "诈骗罪",
    data_classification: str = "审查起诉阶段",
    review_stage: str = "模拟案件",
) -> None:
    """Render the single Swiss editorial case heading shared by the workspaces."""
    if not case_name:
        case_name = _load_case_display_names().get(case_id)
    if not case_name:
        if case_id == "GOLD_CASE_001":
            title = "何某涉嫌诈骗案"
            sub = "资金证据审查"
            if data_classification == "演示案件":
                data_classification = "实战评测卷宗"
            if case_type == "诈骗案件":
                case_type = "诈骗罪"
        elif case_id in {"CASE-0001", "DEMO_CASE_001", "D01"}:
            title = "何某等涉嫌诈骗案（演示案例）"
            sub = "资金证据核验"
        else:
            title = f"{case_id} 涉嫌经济犯罪案"
            sub = "资金证据审查"
    else:
        title = case_name
        sub = "资金证据核验"

    st.markdown(
        f'<header class="case-masthead">'
        f'<div class="masthead-top">'
        f'<span class="masthead-case-code">案件编号 / {html.escape(case_id)}</span>'
        f'<span class="masthead-status-badge">{html.escape(status)}</span>'
        f'</div>'
        f'<h1>{html.escape(title)}</h1>'
        f'<p class="masthead-subtitle">{html.escape(sub)}</p>'
        f'<div class="masthead-meta">'
        f'<span>{html.escape(case_type)}</span>'
        f'<span class="meta-dot">·</span>'
        f'<span>{html.escape(data_classification)}</span>'
        f'<span class="meta-dot">·</span>'
        f'<span>{html.escape(review_stage)}</span>'
        f'</div>'
        f'</header>',
        unsafe_allow_html=True,
    )


def render_stat_strip(items: list[tuple[str, str, str]]) -> None:
    cells = "".join(
        f'<div class="stat-item">'
        f'<div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value">{html.escape(value)}</div>'
        f'<div class="stat-note">{html.escape(note)}</div>'
        f'</div>'
        for label, value, note in items
    )
    st.markdown(f'<div class="stat-strip">{cells}</div>', unsafe_allow_html=True)


def render_section_heading(number: str, title: str, subtitle: str | None = None) -> None:
    extra = f'<span class="heading-sub">{subtitle}</span>' if subtitle else ""
    st.markdown(
        f'<div class="section-heading">'
        f'<span class="section-kicker">{number}</span>'
        f'<strong>{title}</strong>'
        f'{extra}'
        f'</div>',
        unsafe_allow_html=True,
    )


STATUS_CONFIG = {
    "SUPPORTED": {"symbol": "✓", "text": "已支持", "tone": "ok", "class": "status-ok"},
    "FULLY_CORROBORATED": {"symbol": "✓", "text": "完整覆盖", "tone": "ok", "class": "status-ok"},
    "PARTIAL": {"symbol": "◐", "text": "部分支持", "tone": "partial", "class": "status-partial"},
    "PARTIALLY_CORROBORATED": {"symbol": "◐", "text": "部分覆盖", "tone": "partial", "class": "status-partial"},
    "CONFLICT": {"symbol": "!", "text": "存在冲突", "tone": "conflict", "class": "status-conflict"},
    "CONFLICTING": {"symbol": "!", "text": "存在冲突", "tone": "conflict", "class": "status-conflict"},
    "INSUFFICIENT": {"symbol": "—", "text": "证据不足", "tone": "insufficient", "class": "status-insufficient"},
    "UNSUPPORTED": {"symbol": "—", "text": "暂未支持", "tone": "insufficient", "class": "status-insufficient"},
    "EXCLUDED": {"symbol": "×", "text": "予以排除", "tone": "excluded", "class": "status-excluded"},
    "PENDING_REVIEW": {"symbol": "◌", "text": "等待人工复核", "tone": "pending", "class": "status-pending"},
    "PENDING": {"symbol": "◌", "text": "等待人工审查", "tone": "pending", "class": "status-pending"},
    "HUMAN_CONFIRMED": {"symbol": "✓", "text": "人工复核确认", "tone": "ok", "class": "status-ok"},
    "SYSTEM_PROPOSED": {"symbol": "◌", "text": "系统初始建议", "tone": "pending", "class": "status-pending"},
}

STATUS_LABELS = {k: f"{v['symbol']} {v['text']}" for k, v in STATUS_CONFIG.items()}


def render_status_label(label: str, tone: str = "neutral") -> str:
    if label in STATUS_CONFIG:
        cfg = STATUS_CONFIG[label]
    else:
        cfg = {"symbol": "·", "text": label, "class": f"status-{tone}"}
    return f'<span class="accessible-status {cfg["class"]}"><span class="status-symbol">{cfg["symbol"]}</span> {cfg["text"]}</span>'


def _status_label(value: str) -> str:
    if value in STATUS_CONFIG:
        return f"{STATUS_CONFIG[value]['symbol']} {STATUS_CONFIG[value]['text']}"
    return STATUS_LABELS.get(value, value)


def _status_class(value: str) -> str:
    if value in {"CONFLICTING", "CONFLICT"}:
        return "conflict"
    if value in {"PENDING_REVIEW", "PENDING", "SYSTEM_PROPOSED"}:
        return "pending"
    if value in {"FULLY_CORROBORATED", "SUPPORTED", "HUMAN_CONFIRMED"}:
        return "ok"
    if value in {"PARTIALLY_CORROBORATED", "PARTIAL"}:
        return "partial"
    if value in {"EXCLUDED"}:
        return "excluded"
    return "insufficient"


def render_source_quote(source: str, text: str, locator: str = "") -> None:
    st.markdown(
        f'<figure class="source-quote">'
        f'<div class="source-kicker">卷宗材料出处 / {html.escape(source)}</div>'
        f'<blockquote>“{html.escape(text)}”</blockquote>'
        f'<figcaption>{html.escape(locator)}</figcaption>'
        f'</figure>',
        unsafe_allow_html=True,
    )


def render_review_step_indicator(step1_done: bool, step2_done: bool, step3_done: bool) -> None:
    s1_cls = "done" if step1_done else "active"
    s1_sym = "✓" if step1_done else "01"
    s2_cls = "done" if step2_done else ("active" if step1_done else "idle")
    s2_sym = "✓" if step2_done else "02"
    s3_cls = "done" if step3_done else ("active" if step2_done else "idle")
    s3_sym = "✓" if step3_done else "03"

    st.markdown(
        f'<nav class="step-nav" aria-label="审查阶段流转">'
        f'<div class="step-nav-item {s1_cls}"><span class="step-num">{s1_sym}</span><span>核准指控事实主张</span></div>'
        f'<div class="step-nav-divider">──➔</div>'
        f'<div class="step-nav-item {s2_cls}"><span class="step-num">{s2_sym}</span><span>穿透核验候选流水</span></div>'
        f'<div class="step-nav-divider">──➔</div>'
        f'<div class="step-nav-item {s3_cls}"><span class="step-num">{s3_sym}</span><span>签署复核底稿确认</span></div>'
        f'</nav>',
        unsafe_allow_html=True,
    )


def render_review_issue(issue_id: str, title: str, status: str, materials: list[dict], conclusion: str, next_action: str) -> None:
    rows = "".join(
        f'<div class="material-col">'
        f'<small>{html.escape(m.get("source", "材料"))}</small>'
        f'<p class="material-quote">“{html.escape(m.get("finding", "-"))}”</p>'
        f'</div>'
        for m in materials
    )
    st.markdown(
        f'<article class="review-issue">'
        f'<div class="issue-kicker">'
        f'<span>02 / 冲突焦点 · {issue_id}</span>'
        f'<span class="accessible-status status-conflict">{status}</span>'
        f'</div>'
        f'<h3>{html.escape(title)}</h3>'
        f'<div class="issue-materials">{rows}</div>'
        f'<div class="issue-conclusion"><b>当前核验发现</b><p>{html.escape(conclusion)}</p></div>'
        f'<div class="issue-next"><b>下一步 · 回查事项</b><p>{html.escape(next_action)}</p></div>'
        f'</article>',
        unsafe_allow_html=True,
    )


def render_high_risk_transaction(priority: str, tx, candidate) -> None:
    st.markdown(
        f'<article class="risk-panel">'
        f'<div class="risk-kicker">'
        f'<span>{html.escape(priority)} · 高风险交易</span>'
        f'<span class="accessible-status status-conflict">! 待核查阻断项</span>'
        f'</div>'
        f'<h3>▌ {html.escape(tx.transaction_id)} <strong class="risk-amount">¥{tx.amount:,.2f}</strong></h3>'
        f'<div class="risk-grid">'
        f'<div><small>交易日期</small>{html.escape(str(tx.date))}</div>'
        f'<div><small>付款人</small>{html.escape(tx.payer_name or "-")}</div>'
        f'<div><small>收款人</small>{html.escape(tx.payee_name or "-")}</div>'
        f'<div><small>来源定位</small>{html.escape(_source_locator_label(tx))}</div>'
        f'</div>'
        f'<p class="risk-reason"><strong>风险说明：</strong>{html.escape(_risks_to_chinese(candidate.risk_codes))}</p>'
        f'</article>',
        unsafe_allow_html=True,
    )


def render_review_summary(title: str, text: str) -> None:
    st.markdown(
        f'<div class="review-summary">'
        f'<div class="section-kicker">审查备忘</div>'
        f'<h2>{html.escape(title)}</h2>'
        f'<p>{html.escape(text)}</p>'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_claim_review_items(claims_list, result, decision) -> None:
    render_section_heading("02 / 指控清单", "指控事实与核验状态", "逐笔主张对应状态及采纳依据")
    decisions_dict = getattr(result, "system_decisions_by_claim", {}) or {}

    html_items = []
    for idx, c in enumerate(claims_list, 1):
        c_decision = decisions_dict.get(c.id) if not decision or getattr(decision, "claim_id", None) != c.id else decision
        status_val = c_decision.status.value if c_decision else "PENDING_REVIEW"

        candidates = result.candidates_by_claim.get(c.id, []) if getattr(result, "candidates_by_claim", None) else result.candidates
        has_conflict = any(cand.blocking_conflict or "THIRD_PARTY_RECIPIENT" in cand.risk_codes for cand in candidates)
        if has_conflict and status_val != "FULLY_CORROBORATED":
            status_val = "CONFLICTING"

        cfg = STATUS_CONFIG.get(status_val, {"symbol": "·", "text": status_val, "class": "status-neutral"})
        claim_code = f"C{idx:03d}"
        desc = f"被害人 {html.escape(c.victim_name)} 向 {html.escape(c.alleged_recipient_name or '指定账户')} 支付涉案款项 ({c.time_start} 至 {c.time_end})"

        html_items.append(
            f'<div class="claim-item-row">'
            f'<div class="claim-item-meta">'
            f'<span class="claim-item-code">{html.escape(claim_code)} · {html.escape(c.id)}</span>'
            f'<span class="accessible-status {cfg["class"]}"><span class="status-symbol">{cfg["symbol"]}</span> {html.escape(cfg["text"])}</span>'
            f'</div>'
            f'<div class="claim-item-title">{desc}</div>'
            f'<div class="claim-item-amount">¥{c.claimed_amount:,.2f}</div>'
            f'</div>'
        )

    st.markdown('<div class="claim-items-list">' + "".join(html_items) + '</div>', unsafe_allow_html=True)


RULE_LABELS = {
    "M01": "付款账号精准吻合",
    "M02": "付款人姓名匹配",
    "M03": "收款账号精准吻合",
    "M04": "收款人姓名匹配",
    "M05": "日期在指控期内",
    "M06": "日期在容差期内",
    "M07": "金额完全吻合",
    "M08": "分笔部分支付",
    "M09": "单笔超出指控",
}

def _rules_to_chinese(rules) -> str:
    if not rules:
        return "基本规则比对"
    return "、".join(RULE_LABELS.get(r, r) for r in rules)

RISK_LABELS = {
    "THIRD_PARTY_RECIPIENT": "第三方代收(非嫌疑人开户)",
    "PAYER_ACCOUNT_MISMATCH": "付款账号不一致",
    "PAYEE_ACCOUNT_MISMATCH": "收款账号不一致",
    "AMOUNT_EXCEEDS_CLAIM": "单笔金额超出指控数额",
    "DUPLICATE_TRANSACTION": "疑似重复/镜像记录",
    "STATEMENT_AMOUNT_CONFLICT": "笔录金额与起诉书矛盾",
    "STATEMENT_DATE_CONFLICT": "笔录日期与起诉书矛盾",
    "STATEMENT_RECIPIENT_CONFLICT": "笔录收款人与起诉书矛盾",
    "CROSS_CLAIM_DUPLICATION": "跨主张重复充抵冲突",
}

def _risks_to_chinese(risks) -> str:
    if not risks:
        return "未发现异常风险"
    return "；".join(RISK_LABELS.get(r, r) for r in risks)

DISPOSITION_CN = {
    "采信纳入 (计入涉案数额)": "INCLUDED",
    "列为争议 (存疑代收/待查)": "DISPUTED",
    "予以排除 (无关/错误流水)": "EXCLUDED",
    "等待人工审查": "PENDING",
}
DISPOSITION_TO_CN = {v: k for k, v in DISPOSITION_CN.items()}

REASON_CN = {
    "吻合起诉指控事实": "MATCHED_CLAIM",
    "第三方账户代收代转": "THIRD_PARTY_RECIPIENT",
    "重复记账/镜像流水": "DUPLICATE_TRANSACTION",
    "与本案无关的日常交易": "UNRELATED_TRANSACTION",
    "非指定涉案银行账户": "ACCOUNT_MISMATCH",
    "金额与指控存在出入": "AMOUNT_MISMATCH",
    "超出案发时间跨度": "DATE_MISMATCH",
    "其他经办人说明事项": "OTHER",
}
REASON_TO_CN = {v: k for k, v in REASON_CN.items()}


def _apply_batch_dispositions(candidates, state_disp_key, state_reason_key, mode: str) -> str:
    """批量改写处置状态，返回给用户的可见回执文案。"""
    adopted = 0
    disputed = 0
    for c in candidates:
        if mode == "reset":
            if c.blocking_conflict:
                st.session_state[state_disp_key][c.transaction_id] = "列为争议 (存疑代收/待查)"
                st.session_state[state_reason_key][c.transaction_id] = "第三方账户代收代转"
                disputed += 1
            else:
                st.session_state[state_disp_key][c.transaction_id] = "等待人工审查"
                st.session_state[state_reason_key][c.transaction_id] = None
            continue
        is_disputed = c.blocking_conflict or (mode == "smart" and "THIRD_PARTY_RECIPIENT" in c.risk_codes)
        if is_disputed:
            st.session_state[state_disp_key][c.transaction_id] = "列为争议 (存疑代收/待查)"
            st.session_state[state_reason_key][c.transaction_id] = "第三方账户代收代转"
            disputed += 1
        else:
            st.session_state[state_disp_key][c.transaction_id] = "采信纳入 (计入涉案数额)"
            st.session_state[state_reason_key][c.transaction_id] = "吻合起诉指控事实"
            adopted += 1
    if mode == "reset":
        return f"已恢复系统建议：{disputed} 笔阻断候选保留为争议，其余 {len(candidates) - disputed} 笔回到等待人工审查。"
    notice = f"✓ 已处理 {adopted} 笔常规候选"
    if disputed:
        notice += f"；{disputed} 笔阻断/第三方代收候选保留为争议，仍需人工审查"
    return notice + "。"


def _render_candidate_batch_toolbar(candidates, state_disp_key, state_reason_key, editor_state_key, claim_id, suffix) -> None:
    """批量处置工具栏：候选区顶部与签署区底部各放一组，按钮行为完全一致。"""
    col_b1, col_b2, col_b3 = st.columns(3)
    if col_b1.button(
        "批量采纳常规候选", use_container_width=True, key=f"batch_adopt_{suffix}_{claim_id}",
        help="采纳无阻断风险的候选；高风险阻断项自动保留为【列为争议】",
    ):
        st.session_state[f"batch_notice_{claim_id}"] = _apply_batch_dispositions(
            candidates, state_disp_key, state_reason_key, "adopt"
        )
        st.session_state.pop(editor_state_key, None)
        st.rerun()
    if col_b2.button(
        "智能预填：第三方代收列争议，其余采纳", use_container_width=True, key=f"batch_smart_{suffix}_{claim_id}",
        help="自动识别第三方非嫌疑人开户的流水标记为【列为争议】，其余流水置为【采信纳入】",
    ):
        st.session_state[f"batch_notice_{claim_id}"] = _apply_batch_dispositions(
            candidates, state_disp_key, state_reason_key, "smart"
        )
        st.session_state.pop(editor_state_key, None)
        st.rerun()
    if col_b3.button(
        "恢复系统建议", use_container_width=True, key=f"batch_reset_{suffix}_{claim_id}",
        help="清除人工改动：阻断候选回到【列为争议】，其余候选回到【等待人工审查】",
    ):
        st.session_state[f"batch_notice_{claim_id}"] = _apply_batch_dispositions(
            candidates, state_disp_key, state_reason_key, "reset"
        )
        st.session_state.pop(editor_state_key, None)
        st.rerun()


def _show_batch_notice(claim_id, *, pop: bool = False) -> None:
    """批量操作回执：一次性展示，不依赖数据表肉眼变化。候选区顶部与签署区各展示一次后清除。"""
    key = f"batch_notice_{claim_id}"
    notice = st.session_state.get(key)
    if notice:
        st.success(notice)
        if pop:
            st.session_state.pop(key, None)



def _evidence_card(title: str, items: list[tuple[str, str]], badge: tuple[str, str] | None = None) -> None:
    badge_html = f'<span class="accessible-status status-{html.escape(badge[1])}">{html.escape(badge[0])}</span>' if badge else ""
    cells = "".join(
        f'<div class="evidence-cell">'
        f'<span class="evidence-label">{html.escape(label)}</span>'
        f'<span class="evidence-value">{html.escape(value)}</span>'
        f'</div>'
        for label, value in items
    )
    st.markdown(
        f'<article class="evidence-card">'
        f'<div class="evidence-header">'
        f'<h4>{html.escape(title)}</h4>'
        f'{badge_html}'
        f'</div>'
        f'<div class="evidence-grid">{cells}</div>'
        f'</article>',
        unsafe_allow_html=True,
    )


def _render_case_query(result, *, claim_id=None, transaction_id=None, entity=None, key="case_query") -> None:
    from case_query_panel import render_case_query_panel
    render_case_query_panel(
        result, supplementary_documents=_supplementary_documents(result),
        claim_id=claim_id, transaction_id=transaction_id, entity=entity, key=key,
    )



def _editor_records(edited) -> list[dict]:
    if hasattr(edited, "to_dict"):
        return edited.to_dict("records")
    return list(edited)


def _source_locator_label(tx) -> str:
    source = tx.source_evidence_id or "未知证据"
    account = f" / {tx.source_account_id}" if tx.source_account_id else ""
    return f"{source}{account} / 第{tx.source_row}行"


def _checklist_csv(items: list[dict]) -> str:
    output = io.StringIO()
    fields = ["item_id", "priority", "status", "category", "target", "source_locator", "suggestion", "next_action"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(items)
    return output.getvalue()


def _apply_checklist_statuses(case_id: str, items: list[dict]) -> list[dict]:
    """Keep follow-up review state in the current case session and exports."""
    state_key = f"investigation_status_{case_id}"
    statuses = st.session_state.setdefault(state_key, {})
    updated: list[dict] = []
    for item in items:
        item_id = item.get("item_id", "")
        current = statuses.get(item_id, item.get("status", "待核查"))
        checked = st.checkbox(
            f"{item.get('priority', '')}优先 · {item.get('category', '')}",
            value=current == "已核查",
            key=f"{state_key}_{item_id}",
        )
        current = "已核查" if checked else "待核查"
        statuses[item_id] = current
        copy = dict(item)
        copy["status"] = current
        updated.append(copy)
        st.caption(
            f"{item.get('target', '')} · 原始证据：{item.get('source_locator', '未提供定位')}\n\n"
            f"建议：{item.get('suggestion', '')} 下一步：{item.get('next_action', '')}"
        )
    return updated


def _landing_page() -> None:
    """无案件首页：不是任何具体案件的 Masthead，只提供入口与最近案件。"""
    st.markdown(
        '<header class="case-masthead landing-masthead">'
        '<div class="masthead-top">'
        '<span class="masthead-case-code">资金链证审</span>'
        '<span class="masthead-status-badge">尚未加载案件</span>'
        '</div>'
        '<h1>资金证据审查系统</h1>'
        '<div class="masthead-meta">'
        '<span>起诉书 · 被害人陈述 · 银行流水交叉核验</span>'
        '<span class="meta-dot">·</span>'
        '<span>金额计算与状态判定全部由确定性规则完成</span>'
        '</div>'
        '</header>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="legal-notice"><strong>边界说明：</strong>本系统只核验已登记材料之间的资金证据对应关系，'
        '不作定罪、量刑或最终犯罪金额认定；审查结论须经人工复核签署后生效。演示案件仅供功能体验，不代表您正在办理的案件。</div>',
        unsafe_allow_html=True,
    )

    database_path = ROOT / "data" / "cases.db"
    recent_cases: list[dict] = []
    signed_case_ids: set[str] = set()
    if database_path.exists():
        with closing(connect(database_path)) as connection:
            recent_cases = Repository(connection).list_cases()
            signed_case_ids = {
                row["case_id"]
                for row in connection.execute(
                    "SELECT DISTINCT case_id FROM decisions "
                    "WHERE json_extract(payload_json, '$.decision_type') = 'HUMAN_CONFIRMED'"
                )
            }
    display_names = _load_case_display_names()

    if recent_cases:
        render_section_heading("01 / 最近案件", "最近案件", "点击直接恢复上次审查进度")
        for case in recent_cases[:6]:
            cid = case["case_id"]
            title = f"{display_names[cid]}（{cid}）" if display_names.get(cid) else cid
            status = "✓ 复核已签署" if cid in signed_case_ids else "◌ 等待人工复核"
            col_info, col_open = st.columns([4, 1])
            col_info.markdown(
                f'<div style="border-bottom:1px solid var(--line);padding:8px 0;">'
                f'<strong>{html.escape(title)}</strong>'
                f'<span style="color:var(--muted);font-size:14.5px;">'
                f'　主张 {case["claim_count"]} 笔 · 流水 {case["tx_count"]} 笔 · {status}</span></div>',
                unsafe_allow_html=True,
            )
            if col_open.button("打开", key=f"landing_open_{cid}", use_container_width=True):
                if _restore_case_into_session(database_path, cid):
                    st.query_params["case_id"] = cid
                    st.session_state.pop("landing_panel", None)
                    st.session_state["sidebar_notice"] = f"已打开历史案件：{title}"
                    st.rerun()
                else:
                    st.error("该案件未读取到有效主张或流水数据。")

    render_section_heading("02 / 开始", "开始审查", "新建、演示或调阅历史案件")
    col_new, col_demo, col_history = st.columns(3)
    with col_new:
        with st.container(border=True):
            st.markdown("**新建案件**")
            st.caption("上传起诉书、被害人陈述、银行流水等材料，建立全新审查工作区。")
            if st.button("上传案卷材料", key="landing_new", use_container_width=True):
                st.session_state["material_source"] = "上传材料"
                st.session_state["landing_panel"] = True
                st.rerun()
    with col_demo:
        with st.container(border=True):
            st.markdown("**打开演示案件**")
            st.caption("演示案例为完全虚构的快速演示；实战评测案例为 736.8 万评测卷宗。")
            if st.button("打开演示案例", key="landing_demo_d01", use_container_width=True):
                st.session_state["material_source"] = "演示案例"
                st.session_state["landing_panel"] = True
                st.rerun()
            if st.button("实战评测案例（736.8万）", key="landing_demo_gold", use_container_width=True):
                st.session_state["material_source"] = "实战评测案例（736.8万）"
                st.session_state["landing_panel"] = True
                st.rerun()
    with col_history:
        with st.container(border=True):
            st.markdown("**打开历史案件**")
            st.caption("调阅本机 SQLite 中保存的脱敏案件快照与复核进度。")
            if st.button("调阅历史案件", key="landing_history", use_container_width=True, disabled=not recent_cases):
                st.session_state["material_source"] = "打开历史案件"
                st.session_state["landing_panel"] = True
                st.rerun()
            if not recent_cases:
                st.caption("暂无已保存的历史案件。")

    if st.session_state.get("landing_panel"):
        st.divider()
        _materials_panel("CASE-0001")


def case_page() -> None:
    current_result = st.session_state.get("result")
    if current_result is None:
        _landing_page()
        return
    active_case_id = current_result.claim.case_id
    data_label = "实战评测案例" if active_case_id == "GOLD_CASE_001" else "演示案件"
    update_label = "已恢复本机签署快照" if st.session_state.get("decision") else "等待新操作"
    render_case_masthead(active_case_id, status=update_label, data_classification=data_label)
    _case_overview()
    with st.expander("材料管理 · 新建或切换案件", expanded=st.session_state.pop("materials_panel_expanded", False)):
        _materials_panel(active_case_id)
    st.markdown('<div class="legal-notice"><strong>司法证据核验声明：</strong>本系统只核验当前材料之间的资金证据对应关系，不作定罪、量刑或最终犯罪金额认定。</div>', unsafe_allow_html=True)


def _materials_panel(active_case_id: str) -> None:
    database_path = ROOT / "data" / "cases.db"
    suggested_case_id = _next_available_case_id(database_path, base=active_case_id)
    if suggested_case_id != active_case_id:
        st.info(f"案件编号 {active_case_id} 已存在历史案件记录，已为您分配新案件编号 {suggested_case_id}。")
    case_id = st.text_input("案件编号", value=suggested_case_id)
    persist_locally = st.checkbox("保存脱敏后的本地案件记录", value=False, help="默认不保存上传材料；启用后仅写入本机 SQLite。")
    source_default = "实战评测案例（736.8万）" if active_case_id == "GOLD_CASE_001" else "演示案例"
    if active_case_id not in {"CASE-0001"} and active_case_id != "GOLD_CASE_001":
        source_default = "打开历史案件"
    source_options = ["演示案例", "实战评测案例（736.8万）", "上传材料", "打开历史案件"]
    if "material_source" in st.session_state:
        source = st.segmented_control("材料来源", source_options, key="material_source")
    else:
        source = st.segmented_control("材料来源", source_options, default=source_default, key="material_source")
    if source == "演示案例":
        st.caption("使用完全虚构的演示案例：指控50,000元，流水对应30,000元。")
        run_clicked = st.button("运行演示审查", type="primary", width="content")
        if run_clicked:
            st.query_params.pop("case_id", None)
            try:
                with st.status("正在执行审查工作流", expanded=True) as status:
                    result = _load_demo(provider_from_environment(provider_name))
                    st.write("起诉书事实主张提取完成")
                    st.write("被害人陈述交叉核对完成")
                    st.write(f"银行流水解析完成：{len(result.transactions)} 笔")
                    st.write(f"候选交易召回完成：{len(result.candidates)} 笔")
                    status.update(label="审查任务等待人工复核", state="complete")
                st.session_state.result = result
                st.session_state.supplementary_documents = []
                st.session_state.repository_path = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"审查工作流失败：{exc}")
    elif source == "实战评测案例（736.8万）":
        st.caption("载入高难度评测案卷：涉案总额 7,368,000 元，直接读取原始 Excel 多账户流水（招行/工行/证券）及第三方代收（林某 A005）。")
        col_g1, col_g2 = st.columns([1, 3])
        with col_g1:
            run_gold = st.button("启动实战全案审查", type="primary")
        with col_g2:
            st.info("提示：评审现场可选择左侧【DeepSeek API】实测大模型长卷宗语义提取，或使用【本地 Mock】极速演示。")
        if run_gold:
            st.query_params.pop("case_id", None)
            try:
                with st.status("正在加载实战评测卷宗并执行穿透核验...", expanded=True) as status:
                    pkg = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_001"
                    indictment_bytes = (pkg / "visible" / "documents" / "01_起诉书.docx").read_bytes()
                    statement_bytes = (pkg / "visible" / "documents" / "05_被害人陈述.docx").read_bytes()
                    st.write("1. 解析 DOCX 起诉书全文...")
                    indictment_text = extract_document_text(indictment_bytes, filename="01_起诉书.docx")
                    st.write("2. 解析 DOCX 被害人询问笔录...")
                    statement_text = extract_document_text(statement_bytes, filename="05_被害人陈述.docx")
                    supplementary_records = []
                    for filename, label in (("03_证人证言.docx", "证人证言"), ("04_被告人供述与辩解.docx", "被告人供述与辩解")):
                        st.write(f"2.{len(supplementary_records) + 1} 登记 {label}...")
                        supplement_path = pkg / "visible" / "documents" / filename
                        supplementary_records.append({
                            "filename": filename,
                            "text": extract_document_text(supplement_path.read_bytes(), filename=filename),
                        })
                    st.write("3. 直接解析原始 Excel 银行流水并规范化...")
                    xlsx_bytes = (pkg / "visible" / "bank" / "02_银行流水账单.xlsx").read_bytes()
                    csv_text, csv_skip_stats = extract_transactions_csv_detailed(xlsx_bytes, filename="02_银行流水账单.xlsx")
                    total_skipped = sum(csv_skip_stats.values())
                    if total_skipped > 0:
                        st.warning(
                            f"银行流水有 {total_skipped} 行未导入："
                            f"方向无法识别 {csv_skip_stats.get('invalid_direction', 0)} 行、"
                            f"日期无法解析 {csv_skip_stats.get('invalid_datetime', 0)} 行、"
                            f"金额或对手方缺失 {csv_skip_stats.get('invalid_amount_or_counterparty', 0)} 行，"
                            f"请核对原始文件。"
                        )

                    st.write("4. 运行事实主张抽取与确定性资金穿透对账引擎...")
                    provider = provider_from_environment(provider_name)
                    result = run_case_inputs(
                        indictment_text=indictment_text,
                        statement_text=statement_text,
                        csv_text=csv_text,
                        case_id="GOLD_CASE_001",
                        task_id="TASK-GOLD-001",
                        provider=provider,
                        allow_multiple_claims=True,
                        statement_provider=provider,
                        enable_claim_audit=enable_claim_audit,
                        audit_provider=provider,
                        allow_missing_statement=True,
                        transaction_evidence_id="EVI-BANK-XLSX",
                    )
                    status.update(label=f"实战评测案例审查完成：召回 {len(result.candidates)}/{len(result.transactions)} 笔流水，总额 ¥{result.claim.claimed_amount:,.2f}", state="complete")
                st.session_state.result = result
                st.session_state.supplementary_documents = supplementary_records
                st.session_state.repository_path = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
                st.success("实战评测案卷加载完毕！请前往【证据与资金流水】或【资金证据核验】完成全案复核。")
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"实战案卷处理失败：{exc}")
    elif source == "上传材料":
        st.markdown('<div class="section-kicker">案卷材料导入</div>', unsafe_allow_html=True)
        st.caption("按材料类型分别登记；完成度只统计客观上传状态，不构成材料完整性的法律判断。")
        st.caption("说明：PNG / JPG 图片与扫描型 PDF 依赖 OCR 实验性能力，比赛镜像默认未启用；请优先使用 TXT / DOCX / 文本型 PDF / CSV / XLSX 材料。")

        intake_cards = [
            ("起诉书", True, "TXT / DOCX / PDF / PNG / JPG", "intake_indictment",
             ["txt", "docx", "pdf", "png", "jpg", "jpeg"], False,
             "起诉书全文或节选扫描件。"),
            ("被害人陈述", True, "TXT / DOCX / PDF / PNG / JPG · 可多选", "intake_statements",
             ["txt", "docx", "pdf", "png", "jpg", "jpeg"], True,
             "多被害人案件可多选，系统按“被害人X陈述”标题自动切分。"),
            ("银行流水", True, "CSV / XLSX / XLSM / PDF / PNG / JPG", "intake_bank",
             ["csv", "xlsx", "xlsm", "pdf", "png", "jpg", "jpeg"], False,
             "多账户流水可合并在同一工作簿；扫描件走 OCR 可选链路。"),
            ("其他证据", False, "TXT / DOCX / PDF · 可多选", "intake_supplementary",
             ["txt", "docx", "pdf"], True,
             "证人证言、被告供述、聊天记录、项目资料等。补充材料先完成文字提取并登记来源；当前确定性核验主链仍以起诉书、被害人陈述和银行流水为输入。"),
        ]
        uploaded: dict[str, list] = {}
        for title, required, formats, key, types, multiple, help_text in intake_cards:
            with st.container(border=True):
                badge = (
                    '<span class="accessible-status status-pending">必需</span>'
                    if required else '<span class="accessible-status status-insufficient">可选</span>'
                )
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                    f'<strong style="font-size:17px;">{title}</strong>{badge}</div>',
                    unsafe_allow_html=True,
                )
                st.caption(f"支持格式：{formats}")
                files = st.file_uploader(
                    f"{title}上传入口", type=types, accept_multiple_files=multiple,
                    key=key, help=help_text, label_visibility="collapsed",
                )
                file_list = list(files) if isinstance(files, list) else ([files] if files else [])
                uploaded[title] = file_list
                if file_list:
                    names = "、".join(item.name for item in file_list)
                    st.caption(f"✓ 已登记 {len(file_list)} 个文件：{names}（重新选择即可替换或追加）")

        indictment_files = uploaded["起诉书"]
        statement_files = uploaded["被害人陈述"]
        bank_files = uploaded["银行流水"]
        supplementary_files = uploaded["其他证据"]
        core_groups = (("起诉书", indictment_files), ("被害人陈述", statement_files), ("银行流水", bank_files))
        core_done = sum(1 for _, files in core_groups if files)
        st.markdown(
            f'<div style="border-top:1px solid var(--line);border-bottom:1px solid var(--line);'
            f'padding:10px 0;margin:14px 0 6px;font-size:15.5px;">'
            f'核心材料完成度 <strong>{core_done} / 3</strong>（起诉书 · 被害人陈述 · 银行流水）'
            f'{"；其他证据 " + str(len(supplementary_files)) + " 个" if supplementary_files else ""}</div>',
            unsafe_allow_html=True,
        )
        missing = [name for name, files in core_groups if not files]
        if missing:
            st.caption("尚缺必需材料：" + "、".join(missing) + "。")

        if st.button("开始解析案卷", type="primary", disabled=bool(missing)):
            indictment = indictment_files[0]
            statement = statement_files
            supplementary = supplementary_files
            transactions = bank_files[0]
            st.query_params.pop("case_id", None)
            try:
                indictment_text = extract_document_text(indictment.getvalue(), filename=indictment.name)
                statement_text = "\n\n".join(
                    extract_document_text(item.getvalue(), filename=item.name)
                    for item in statement
                )
                supplementary_records = [
                    {"filename": item.name, "text": extract_document_text(item.getvalue(), filename=item.name)}
                    for item in (supplementary or [])
                ]
                provider = provider_from_environment(provider_name)
                csv_text, csv_skip_stats = extract_transactions_csv_detailed(transactions.getvalue(), filename=transactions.name)
                total_skipped = sum(csv_skip_stats.values())
                if total_skipped > 0:
                    st.warning(
                        f"银行流水有 {total_skipped} 行未导入："
                        f"方向无法识别 {csv_skip_stats.get('invalid_direction', 0)} 行、"
                        f"日期无法解析 {csv_skip_stats.get('invalid_datetime', 0)} 行、"
                        f"金额或对手方缺失 {csv_skip_stats.get('invalid_amount_or_counterparty', 0)} 行，"
                        f"请核对原始文件。"
                    )
                result = run_case_inputs(
                    indictment_text=indictment_text,
                    statement_text=statement_text,
                    csv_text=csv_text,
                    case_id=case_id,
                    task_id=f"TASK-{case_id}",
                    provider=provider,
                    allow_multiple_claims=True,
                    statement_provider=provider,
                    enable_claim_audit=enable_claim_audit,
                    audit_provider=provider,
                    allow_missing_statement=True,
                    transaction_evidence_id=f"EVI-BANK-{transactions.name.rsplit('.', 1)[-1].upper()}",
                )
                st.session_state.result = result
                st.session_state.supplementary_documents = supplementary_records
                st.session_state.repository_path = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
                st.success("材料处理完成，等待人工复核。")
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"材料处理失败：{exc}")
    else:
        st.caption("从本地 SQLite 数据库中调阅已保存的历史案件与复核进度。")
        data_dir = ROOT / "data"
        db_path = data_dir / "cases.db"
        if not db_path.exists():
            st.info("本地数据库暂无保存的历史案件。请先通过“演示案例”或“上传材料”创建，并勾选“保存脱敏后的本地案件记录”。")
        else:
            with closing(connect(db_path)) as connection:
                repo = Repository(connection)
                cases = repo.list_cases()
            if not cases:
                st.info("本地数据库中尚无保存的历史案件。")
            else:
                case_labels = {
                    f"{c['case_id']}（主张: {c['claim_count']} 笔，流水: {c['tx_count']} 笔，复核记录: {c['decision_count']} 项）": c["case_id"]
                    for c in cases
                }
                selected_label = st.selectbox("选择历史案件", list(case_labels.keys()))
                selected_case_id = case_labels[selected_label]
                if st.button("载入并恢复案件", type="primary"):
                    if not _restore_case_into_session(db_path, selected_case_id):
                        st.error("该案件未读取到有效主张或流水数据。")
                    else:
                        st.query_params["case_id"] = selected_case_id
                        st.success(f"案件 {selected_case_id} 恢复完成！")
                        st.rerun()
    failed_events = st.session_state.get("failed_audit_events")
    if failed_events:
        with st.expander("失败步骤日志"):
            st.dataframe([event.to_dict() for event in failed_events], width="stretch", hide_index=True)


def _case_overview() -> None:
    result = st.session_state.get("result")
    if result is not None:
        decision = st.session_state.get("decision")
        claims_list = result.claims if result.claims else [result.claim]
        total_claimed = sum((c.claimed_amount for c in claims_list), Decimal("0")) if claims_list else Decimal("0")
        covered = decision.covered_amount if decision else Decimal("0")

        # Show the shared relationship-based return set as a review reference.
        refund_txs = identify_refund_transactions(claims_list, result.transactions.values())
        refund_total = sum((tx.amount for tx in refund_txs), Decimal("0"))

        net_claimed = max(total_claimed - refund_total, Decimal("0"))
        total_candidates = sum(len(cands) for cands in result.candidates_by_claim.values()) if result.candidates_by_claim else len(result.candidates)

        render_section_heading("01 / 总览", "全案资金证据概览", "指控金额、流水核验与审查事项")
        if refund_total > 0:
            render_stat_strip([
                ("指控金额", f"¥{total_claimed:,.2f}", "起诉书主张"),
                ("已返还 (待查)", f"¥{refund_total:,.2f}", "案发前返还流水"),
                ("未返还差额", f"¥{net_claimed:,.2f}", "当前参考差额"),
                ("待复核事项", f"{total_candidates if not decision else 0:02d}", "审查事项"),
            ])
            st.info(f"【疑似返还流水提示】按账户关系和唯一交易事件识别到 {len(refund_txs)} 笔、¥{refund_total:,.2f} 元可能转回被害人账户；摘要不能单独证明收益、分红或法定冲减性质，净额仅作待核验参考（¥{net_claimed:,.2f}）。")
        else:
            render_stat_strip([
                ("指控金额", f"¥{total_claimed:,.2f}", "起诉书主张"),
                ("已核验金额", f"¥{covered:,.2f}", "银行流水确认"),
                ("尚待核验", f"¥{max(total_claimed - covered, Decimal('0')):,.2f}", "当前差额"),
                ("待复核事项", f"{total_candidates if not decision else 0:02d}", "审查事项"),
            ])

        supplementary_documents = st.session_state.get("supplementary_documents", [])
        if supplementary_documents:
            st.caption(
                "已登记补充材料：" + "、".join(item["filename"] for item in supplementary_documents)
                + "；这些材料已完成文字提取并保留来源名称，当前确定性金额核验仍以主链材料为准。"
            )

        render_claim_review_items(claims_list, result, decision)

        render_section_heading("03 / 流程留痕", "审查执行链条与留痕", "确定性算法与经办人复核状态")
        steps = ["材料登记", "付款主张提取", "陈述交叉核验", "银行流水解析", "候选交易召回"]
        completed = {event.step for event in result.audit_events}
        progress_rows = []
        for label, key in zip(steps, ["claim_extraction", "statement_comparison", "transaction_parser", "transaction_parser", "candidate_matcher"]):
            progress_rows.append({"状态": "✓ 已完成" if key in completed else "— 待处理", "审查步骤": label})
        progress_rows.extend([
            {"状态": "✓ 已完成" if decision else "👉 当前步骤", "审查步骤": "人工复核"},
            {"状态": "✓ 已生成" if decision else "— 待处理", "审查步骤": "审查结论与留痕"},
        ])
        st.dataframe(progress_rows, width="stretch", hide_index=True, column_config={"状态": st.column_config.TextColumn(width="small")})

        if not decision:
            st.markdown(
                f'<div style="text-align:right;margin:20px 0 10px;">'
                f'<span style="font-size:15.5px;color:var(--navy);font-weight:700;letter-spacing:0.04em;">'
                f'{total_candidates:02d} 笔流水等待人工审查 → 请前往【03 资金证据核验】'
                f'</span>'
                f'</div>',
                unsafe_allow_html=True,
            )


def transactions_page(result) -> None:
    status_label = "◌ 等待人工复核" if "decision" not in st.session_state else "✓ 复核已完成"
    data_label = "实战评测卷宗" if result.claim.case_id == "GOLD_CASE_001" else "演示案件"
    render_case_masthead(result.claim.case_id, status=status_label, data_classification=data_label, review_stage="涉案资金流水总台账")

    from legal_funds_agent.services.topology_service import build_fund_flow_topology
    claims = result.claims if result.claims else [result.claim]
    decision = st.session_state.get("decision")
    all_candidates = [candidate for candidates in result.candidates_by_claim.values() for candidate in candidates]
    if not all_candidates:
        all_candidates = result.candidates
    candidate_ids = {candidate.transaction_id for candidate in all_candidates}
    refund_txs = identify_refund_transactions(claims, result.transactions.values())
    refund_ids = {tx.id for tx in refund_txs}

    # Core evidence graph: victim payments (claim candidates) + refunds + the
    # main one-hop downstream flows out of accounts that received涉案资金.
    # This keeps the default graph on the evidence story instead of swinging
    # between "candidates only" and "all 100+ rows".
    focus_ids = candidate_ids | refund_ids
    receiving_accounts = {
        tx.payee_account_id
        for tx in result.transactions.values()
        if tx.id in candidate_ids and tx.payee_account_id
    }
    seen_keys = {
        transaction_canonical_key(tx)
        for tx in result.transactions.values()
        if tx.id in focus_ids
    }
    downstream_ids: list[str] = []
    for tx in sorted(
        (tx for tx in result.transactions.values() if tx.payer_account_id in receiving_accounts),
        key=lambda tx: tx.amount,
        reverse=True,
    ):
        key = transaction_canonical_key(tx)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        downstream_ids.append(tx.id)
        if len(downstream_ids) >= 10:
            break
    focus_ids = focus_ids | set(downstream_ids)
    focus_transactions = {
        tx_id: tx for tx_id, tx in result.transactions.items() if tx_id in focus_ids
    }
    focus_transactions = focus_transactions or result.transactions
    topo = build_fund_flow_topology(claims, focus_transactions, decision)
    all_topo = build_fund_flow_topology(claims, result.transactions, decision)

    disputed_names = {
        tx.payee_name
        for candidate in all_candidates
        if "THIRD_PARTY_RECIPIENT" in candidate.risk_codes
        for tx in [result.transactions.get(candidate.transaction_id)]
        if tx and tx.payee_name
    }

    third_party_tx_ids = {
        candidate.transaction_id for candidate in all_candidates
        if "THIRD_PARTY_RECIPIENT" in candidate.risk_codes
    }
    direct_total = sum(
        (tx.amount for tx in result.transactions.values()
         if tx.id in candidate_ids and tx.id not in third_party_tx_ids),
        Decimal("0"),
    )
    third_party_total = sum(
        (tx.amount for tx in result.transactions.values()
         if tx.id in third_party_tx_ids),
        Decimal("0"),
    )

    render_section_heading("01 / 统计", "资金流向与账户穿透总计", "被害人转出与涉案账户接收统计")
    render_stat_strip([
        ("被害人支付总额", f"¥{sum((c.claimed_amount for c in claims), Decimal('0')):,.2f}", "起诉指控总数"),
        ("直接账户收款", f"¥{direct_total:,.2f}", "嫌疑人名下账户"),
        ("第三方代收", f"¥{third_party_total:,.2f}", "非嫌疑人代收待查"),
        ("疑似转回流水", f"¥{sum((tx.amount for tx in refund_txs), Decimal('0')):,.2f}", "待核验法定性质"),
    ])

    render_section_heading("02 / 台账", "涉案银行流水分类台账", "按资金流向、账户关系和规范化标准分流展示")
    st.caption("银行流水按资金方向和账户关系区分为【涉案支付流出】与【疑似转回流水】，所有性质仍需人工核验。")

    refund_keys = {transaction_canonical_key(tx) for tx in refund_txs}

    pay_txs = []
    refund_txs = []

    def transaction_row(tx):
        is_refund = transaction_canonical_key(tx) in refund_keys
        return {
            "交易流水号": tx.transaction_id,
            "交易日期": str(tx.date),
            "交易时间": str(tx.time or "-"),
            "付款人": tx.payer_name or "-",
            "付款账户ID": tx.payer_account_id or "-",
            "付款账号": _mask(tx.payer_account),
            "收款人": tx.payee_name or "-",
            "收款账户ID": tx.payee_account_id or "-",
            "收款账号": _mask(tx.payee_account),
            "交易金额": f"¥ {tx.amount:,.2f}",
            "资金流向性质": "疑似向被害人账户转回（待核验）" if is_refund else ("指控支付 (待核验)" if tx.id in candidate_ids else "日常流水"),
            "原始摘要/备注": tx.remark or "-",
            "来源账单账户ID": tx.source_account_id or "-",
            "卷宗来源行": tx.source_row,
        }

    for tx in unique_transactions(result.transactions.values()):
        item = transaction_row(tx)
        is_refund = transaction_canonical_key(tx) in refund_keys
        if is_refund:
            refund_txs.append((tx, item))
        else:
            pay_txs.append((tx, item))

    all_rows = [(tx, transaction_row(tx)) for tx in result.transactions.values()]

    sum_pay = sum((t[0].amount for t in pay_txs), Decimal("0"))
    sum_refund = sum((t[0].amount for t in refund_txs), Decimal("0"))

    def _tx_search_text(tx) -> str:
        """检索串基于原始字段构建（含未脱敏完整账号），展示列仍为脱敏值。"""
        parts = [
            tx.transaction_id, tx.payer_name, tx.payer_account, tx.payer_account_id,
            tx.payee_name, tx.payee_account, tx.payee_account_id, tx.remark,
            tx.source_row, str(tx.amount), str(tx.date),
        ]
        return " ".join(str(p).lower() for p in parts if p is not None)

    query = st.text_input(
        "快速检索姓名、账号、账户ID或流水号",
        placeholder="例如：朱某、何某、林某、A005、完整银行账号、0022025000009",
        help="三个台账共用同一检索条件；支持完整银行账号（展示表格仍为脱敏值）、姓名、账户ID、流水号、金额与日期。",
    )

    def _filter_txs(pairs):
        if not query:
            return list(pairs)
        needle = query.strip().lower()
        return [(tx, item) for tx, item in pairs if needle in _tx_search_text(tx)]

    pay_txs_filtered = _filter_txs(pay_txs)
    refund_txs_filtered = _filter_txs(refund_txs)
    all_rows_filtered = _filter_txs(all_rows)

    tab_pay, tab_refund, tab_all, tab_graph = st.tabs([
        f"涉案流出支付流水 ({len(pay_txs)} 笔 · ¥{sum_pay:,.2f})",
        f"疑似转回流水 ({len(refund_txs)} 笔 · ¥{sum_refund:,.2f})",
        f"全案银行流水总底册 ({len(all_rows)} 笔)",
        "账户关系图",
    ])

    with tab_pay:
        st.dataframe([t[1] for t in pay_txs_filtered], width="stretch", hide_index=True)
        st.caption(f"筛选命中 {len(pay_txs_filtered)} / 共 {len(pay_txs)} 笔涉案转出流水，其中吻合指控起诉事实主张候选共 {len(candidate_ids)} 笔。")

    with tab_refund:
        st.info(f"【待核验转回流水】按账户关系识别到 {len(refund_txs)} 笔、¥{sum_refund:,.2f} 元可能转入被害人账户；流水摘要不能单独证明返还性质或法定冲减效果。")
        st.dataframe([t[1] for t in refund_txs_filtered], width="stretch", hide_index=True)
        st.caption(f"筛选命中 {len(refund_txs_filtered)} / 共 {len(refund_txs)} 笔。")

    with tab_all:
        st.dataframe([t[1] for t in all_rows_filtered], width="stretch", hide_index=True)
        st.caption(f"筛选命中 {len(all_rows_filtered)} / 共 {len(all_rows)} 笔。全案总计导入 {len(result.transactions)} 笔原始银行记录；前两类台账按规范化唯一事件展示。")

    with tab_graph:
        render_section_heading("03 / 拓扑图", "核心涉案资金流向图", f"从 {len(result.transactions)} 笔原始流水提取主要证据路径：{len(topo.nodes)} 个账户节点、{len(topo.edges)} 条唯一交易事件")
        _render_fund_flow(topo, height=540, key="fund_flow_focus", transactions=result.transactions, disputed_names=disputed_names)
        with st.expander("查看全案资金流向图（全部账户与流水）", expanded=False):
            _render_fund_flow(all_topo, height=560, key="fund_flow_all", transactions=result.transactions, disputed_names=disputed_names)


def review_page(result) -> None:
    status_label = "◌ 等待人工复核" if "decision" not in st.session_state else "✓ 复核已完成"
    data_label = "实战评测卷宗" if result.claim.case_id == "GOLD_CASE_001" else "演示案件"
    render_case_masthead(result.claim.case_id, status=status_label, data_classification=data_label, review_stage="资金证据核验与人工复核")
    claims_list = result.claims if result.claims else [result.claim]
    if len(claims_list) > 1:
        claim_map = {
            f"主张 {idx + 1}：{c.victim_name} ➔ {c.alleged_recipient_name or '待确认'} (¥{c.claimed_amount:,.2f}) [{c.id}]": c
            for idx, c in enumerate(claims_list)
        }
        selected_key = st.selectbox("选择当前核验的涉案付款事实主张", list(claim_map.keys()))
        claim = claim_map[selected_key]
        candidates = result.candidates_by_claim.get(claim.id, [])
        sys_decision = result.system_decisions_by_claim.get(claim.id, result.system_decision)
    else:
        claim = result.claim
        candidates = result.candidates
        sys_decision = result.system_decision

    is_claim_confirmed = (claim.extraction_status == "human_confirmed")
    is_decision_made = ("decision" in st.session_state and getattr(st.session_state.decision, "claim_id", None) == claim.id)

    # 步骤指引导航条
    render_review_step_indicator(is_claim_confirmed, is_decision_made, is_decision_made)

    # Use the same relationship-based, canonical refund set as the other pages.
    refund_list = identify_refund_transactions([claim], result.transactions.values())
    total_refund = sum((t.amount for t in refund_list), Decimal("0"))
    net_claim = max(claim.claimed_amount - total_refund, Decimal("0"))

    if total_refund > 0:
        render_stat_strip([
            ("指控支付总额", f"¥{claim.claimed_amount:,.2f}", "起诉书主张事实"),
            ("疑似转回流水", f"¥{total_refund:,.2f}", "案发前转回待查"),
            ("未返还参考", f"¥{net_claim:,.2f}", "数学参考差额"),
            ("待核候选流水", f"{len(candidates):02d}", "审查事项"),
        ])

        with st.expander(f"【疑似转回流水台账】按账户关系识别 {len(refund_list)} 笔（共计 ¥{total_refund:,.2f}），待人工核验", expanded=True):
            st.info("系统只依据账户关系、方向和唯一交易事件展示候选；摘要中的“收益”“分红”“份额”等文字不能单独证明返还性质或法定冲减效果。")
            r_rows = []
            for idx, r in enumerate(refund_list, 1):
                r_rows.append({
                    "序号": idx,
                    "交易流水号": r.transaction_id,
                    "交易日期": str(r.date),
                    "转出人 (付款方)": f"{r.payer_name} ({_mask(r.payer_account)})",
                    "接收人 (被害人)": f"{r.payee_name} ({_mask(r.payee_account)})",
                    "返还金额": f"¥{r.amount:,.2f}",
                    "原始摘要": r.remark or "-",
                    "当前识别性质": "疑似向被害人账户转回（待核验）",
                })
            st.dataframe(r_rows, width="stretch", hide_index=True)
    else:
        render_stat_strip([
            ("指控涉案金额", f"¥{claim.claimed_amount:,.2f}", "起诉书主张事实"),
            ("召回候选流水", f"{len(candidates):02d}", "银行流水匹配项"),
            ("当前核验状态", _status_label(sys_decision.status.value), "系统建议状态"),
            ("材料冲突标记", f"{len(result.statement_conflicts):02d}", "审查阻断项"),
        ])

    render_section_heading("01 / 事实主张", "起诉事实主张概貌", f"事实主张编号: {claim.id}")
    st.markdown(
        f'<div class="claim-editorial-box">'
        f'<div class="claim-editorial-kicker">起诉书主张 · 涉案金额</div>'
        f'<div class="claim-editorial-amount">¥{claim.claimed_amount:,.2f}</div>'
        f'<p class="claim-editorial-desc">'
        f'起诉指控：被害人 <strong>{claim.victim_name}</strong> 于 {claim.time_start} 至 {claim.time_end} '
        f'按照嫌疑人指示向 <strong>{claim.alleged_recipient_name or "指定涉案账户"}</strong> 支付款项。'
        f'</p>'
        f'</div>',
        unsafe_allow_html=True,
    )

    with st.expander("查看起诉书事实主张原文出处与字符偏移", expanded=False):
        for locator in result.claim_locators:
            render_source_quote("起诉书", locator.source_text or "（无原文片段）", f"{locator.evidence_id} · 字符 {locator.start_offset}–{locator.end_offset}")

    # 步骤一：核准事实主张
    if not is_claim_confirmed:
        st.warning("【第一步：事实主张核准】当前涉案事实主张由规则/大模型初步提取。请复核上述金额、主体与时间范围。确认无误后请点击下方按钮核准，系统将解锁银行流水逐笔核验。")
        if st.button("核准起诉事实主张，进入资金核验", type="primary", use_container_width=True):
            confirmed = confirm_claim_extraction(claim)
            if result.claims:
                for i, c in enumerate(result.claims):
                    if c.id == claim.id:
                        result.claims[i] = confirmed
            if result.claim.id == claim.id:
                result.claim = confirmed
            repository_path = st.session_state.get("repository_path")
            if repository_path:
                try:
                    _save_confirmed_claim(repository_path, confirmed)
                except ValueError as exc:
                    if "immutable claim already exists" in str(exc):
                        st.error("该案号下已有签署存档的资金主张，不可覆盖。请更换案件编号后重新上传材料。")
                        return
                    raise
            st.session_state.pop("decision", None)
            st.session_state.pop("report", None)
            st.rerun()
        return

    st.markdown(
        '<div class="accessible-notice ok"><span class="notice-icon">✓</span> '
        '<strong>步骤一已完成：</strong>该涉案付款事实主张已由办案经办人员人工审核批准。</div>',
        unsafe_allow_html=True,
    )
    if result.statement_conflicts:
        st.markdown(
            f'<div class="accessible-notice danger"><span class="notice-icon">!</span> '
            f'<strong>被害人陈述冲突：</strong>笔录与起诉书存在矛盾（{"、".join([RISK_LABELS.get(r, r) for r in result.statement_conflicts])}）。</div>',
            unsafe_allow_html=True,
        )
    elif "RESTORED_STATEMENT_NOT_AVAILABLE" in result.statement_extraction_warnings:
        st.warning("当前为已保存案件的恢复视图，快照未保存陈述抽取结果；不能据此判断言词证据一致，请回查原始材料。")
    else:
        st.markdown(
            '<div class="accessible-notice ok"><span class="notice-icon">✓</span> '
            '<strong>言词证据一致：</strong>被害人陈述笔录与起诉书在转账金额、时间跨度及收款对象上未发现冲突。</div>',
            unsafe_allow_html=True,
        )
    if result.duplicate_groups:
        groups = list(result.duplicate_groups.values())
        st.markdown(
            f'<div class="accessible-notice danger"><span class="notice-icon">!</span> '
            f'<strong>重复记账/镜像流水预警：</strong>发现 {len(groups)} 组疑似重复记账或镜像流水，请优先核实排除。</div>',
            unsafe_allow_html=True,
        )
        # 每笔流水可能被哪些主张召回为候选
        claim_list = result.claims if result.claims else [result.claim]
        candidate_claims: dict[str, list[str]] = {}
        for c in claim_list:
            for cand in result.candidates_by_claim.get(c.id, []):
                candidate_claims.setdefault(cand.transaction_id, []).append(c.id)
        with st.expander("查看重复/镜像流水组 · 影响点与处置指引", expanded=True):
            st.info(
                "【通俗说明】以下各组是同一笔资金在不同账户账单中各记一次的镜像记录（例如同一转账同时出现在付款方与收款方两份账单里）。"
                "核验时同一组只应把其中一笔采信纳入主张覆盖金额，其余流水请标记为“重复记账/镜像流水”予以排除；"
                "否则覆盖金额会被重复计算。决策引擎已把重复组作为风险码（DUPLICATE_TRANSACTION）自动阻断重复计入，"
                "此处供人工复核确认。"
            )
            summary_rows = []
            detail_rows = []
            for group_index, tx_ids in enumerate(groups, 1):
                group_txs = [result.transactions[tid] for tid in tx_ids if tid in result.transactions]
                if not group_txs:
                    continue
                first = group_txs[0]
                amounts = {t.amount for t in group_txs}
                dates = {str(t.date) for t in group_txs}
                summary_rows.append({
                    "重复组": f"第 {group_index} 组",
                    "金额": f"¥{first.amount:,.2f}" if len(amounts) == 1 else " / ".join(f"¥{a:,.2f}" for a in sorted(amounts)),
                    "日期": " / ".join(sorted(dates)),
                    "付款方": f"{first.payer_name} ({first.payer_account_id or '-'})",
                    "收款方": f"{first.payee_name} ({first.payee_account_id or '-'})",
                    "组内流水数": len(group_txs),
                })
                for tx in group_txs:
                    claims_hit = candidate_claims.get(tx.id, [])
                    claim_tags = "、".join(claims_hit) if claims_hit else "—"
                    detail_rows.append({
                        "重复组": f"第 {group_index} 组",
                        "交易流水号": tx.transaction_id,
                        "候选归属主张": claim_tags,
                        "是否当前主张候选": "是" if tx.id in {c.transaction_id for c in candidates} else "否",
                        "原始证据定位": _source_locator_label(tx),
                    })
            st.markdown('<div class="section-kicker">重复组总览</div>', unsafe_allow_html=True)
            st.dataframe(summary_rows, width="stretch", hide_index=True)
            st.markdown('<div class="section-kicker">组内逐笔定位</div>', unsafe_allow_html=True)
            st.dataframe(detail_rows, width="stretch", hide_index=True)
            st.caption("处置建议：同一组内仅保留一笔作为采信流水，其余在【03 资金证据核验】候选审查表中标记为“重复记账/镜像流水”排除。")

    supplementary_documents = _supplementary_documents(result)
    conflict_matrix = _conflict_matrix_for(result, supplementary_documents)
    render_section_heading("02 / 冲突焦点", "言词证据与流水冲突焦点", "言词材料与资金流水的并列交叉回查")
    if conflict_matrix:
        for conflict in conflict_matrix:
            render_review_issue(conflict["id"], conflict["title"], f"! {conflict['priority']}优先", conflict["materials"], conflict["conclusion"], conflict["next_action"])
    else:
        st.markdown('<div class="review-summary"><div class="section-kicker">证据提示</div><strong>当前没有登记补充言词材料。</strong><p>资金流水核验队列仍可继续处理。</p></div>', unsafe_allow_html=True)

    # Risk-first ordering keeps the audit queue aligned with review necessity.
    candidates = sort_candidates_for_review(candidates, result.transactions)

    # 步骤二：流水核验
    render_section_heading("03 / 候选审查", "待核验候选流水审查表", "按审核必要性与阻断风险智能排序")
    high_risk_candidates = [c for c in candidates if candidate_risk_level(c) == "高"]
    review_order = {candidate.transaction_id: index for index, candidate in enumerate(candidates, 1)}
    st.caption(f"已按审核必要性排序：P1 优先处理阻断风险，其次按风险分、金额和日期排列；重点核查 {len(high_risk_candidates)} 笔，常规候选 {len(candidates) - len(high_risk_candidates)} 笔。")

    if high_risk_candidates:
        render_section_heading("03.1 / 高风险", "高风险阻断交易", "必须逐笔核验，不进入批量采信")
        for candidate in high_risk_candidates:
            tx = result.transactions[candidate.transaction_id]
            render_high_risk_transaction(f"P{review_order[candidate.transaction_id]}", tx, candidate)

    state_disp_key = f"candidate_disps_{claim.id}"
    state_reason_key = f"candidate_reasons_{claim.id}"

    if state_disp_key not in st.session_state:
        st.session_state[state_disp_key] = {
            c.transaction_id: ("列为争议 (存疑代收/待查)" if c.blocking_conflict else "等待人工审查")
            for c in candidates
        }
    if state_reason_key not in st.session_state:
        st.session_state[state_reason_key] = {
            c.transaction_id: ("第三方账户代收代转" if c.blocking_conflict else None)
            for c in candidates
        }

    # Keep legacy widget state from re-enabling a blocking candidate after a
    # refresh or after a previous version used the old batch action.
    for candidate in candidates:
        if candidate.blocking_conflict:
            st.session_state[state_disp_key][candidate.transaction_id] = "列为争议 (存疑代收/待查)"
            st.session_state[state_reason_key][candidate.transaction_id] = "第三方账户代收代转"

    # 快捷批量操作工具栏（候选区顶部一组；签署区底部另有一组相同动作）
    editor_state_key = f"candidate_review_editor_v2_{claim.id}"
    _render_candidate_batch_toolbar(candidates, state_disp_key, state_reason_key, editor_state_key, claim.id, "top")
    _show_batch_notice(claim.id)

    candidate_rows = []
    for candidate in candidates:
        tx = result.transactions[candidate.transaction_id]
        cur_disp = st.session_state[state_disp_key].get(candidate.transaction_id, "等待人工审查")
        cur_reason = st.session_state[state_reason_key].get(candidate.transaction_id, None)
        candidate_rows.append({
            "处置决断": cur_disp,
            "认定理由": cur_reason,
            "经办备注": "",
            "风险等级": ("! 高风险" if candidate_risk_level(candidate) == "高" else ("◐ 中风险" if candidate_risk_level(candidate) == "中" else "— 常规")),
            "审核顺序": f"P{review_order[candidate.transaction_id]}",
            "流水号": tx.transaction_id,
            "交易日期": str(tx.date),
            "付款人": tx.payer_name or "-",
            "付款账户ID": tx.payer_account_id or "-",
            "收款人": tx.payee_name or "-",
            "收款账户ID": tx.payee_account_id or "-",
            "金额": float(tx.amount),
            "核对规则": _rules_to_chinese(candidate.matched_rules),
            "风险提示": _risks_to_chinese(candidate.risk_codes),
            "原始证据定位": _source_locator_label(tx),
            "_tid": tx.id,
        })

    # 左工作表 + 右 Inspector：在右侧选择任意一笔候选流水，固定显示该笔的
    # 证据详情与当前处置，不用在长表中横向滚动寻找上下文。
    col_table, col_tx_inspector = st.columns([2, 1])
    with col_table:
        edited = st.data_editor(
            candidate_rows,
            width="stretch",
            hide_index=True,
            disabled=["风险等级", "审核顺序", "流水号", "交易日期", "付款人", "付款账户ID", "收款人", "收款账户ID", "金额", "核对规则", "风险提示", "原始证据定位", "_tid"],
            column_config={
                "处置决断": st.column_config.SelectboxColumn(options=list(DISPOSITION_CN.keys()), required=True, width="medium"),
                "认定理由": st.column_config.SelectboxColumn(options=list(REASON_CN.keys()), required=True, width="medium"),
                "金额": st.column_config.NumberColumn(format="¥ %.2f", width="small"),
                "审核顺序": st.column_config.TextColumn(width="small"),
                "_tid": None,
            },
            column_order=["风险等级", "审核顺序", "流水号", "交易日期", "金额", "付款人", "收款人", "风险提示", "核对规则", "原始证据定位", "处置决断", "认定理由", "经办备注"],
            key=editor_state_key,
        )

    with col_tx_inspector:
        inspector_options = ["（未选择）"] + [
            f"{row['审核顺序']} · {row['流水号']} · ¥{row['金额']:,.2f}" for row in candidate_rows
        ]
        picked = st.selectbox(
            "流水详情查看",
            inspector_options,
            key=f"tx_inspector_{claim.id}",
            help="选择一笔候选流水，下方显示其证据详情；处置仍在左侧审查表完成。",
        )
        picked_index = inspector_options.index(picked) - 1
        if picked_index < 0:
            st.caption("从上方选择一笔候选流水，此处显示其证据详情与当前处置。")
        else:
            sel_row = candidate_rows[picked_index]
            sel_tx = result.transactions[sel_row["_tid"]]
            sel_candidate = next((c for c in candidates if c.transaction_id == sel_row["_tid"]), None)
            _evidence_card(f"流水详情 · {sel_tx.transaction_id}", [
                ("金额 / 日期", f"¥{sel_tx.amount:,.2f} · {sel_tx.date}"),
                ("付款方", f"{sel_tx.payer_name or '-'}（{sel_tx.payer_account_id or '-'}）"),
                ("收款方", f"{sel_tx.payee_name or '-'}（{sel_tx.payee_account_id or '-'}）"),
                ("核对规则", _rules_to_chinese(sel_candidate.matched_rules) if sel_candidate else "-"),
                ("风险提示", _risks_to_chinese(sel_candidate.risk_codes) if sel_candidate else "-"),
                ("原始证据定位", _source_locator_label(sel_tx)),
                ("当前处置", str(sel_row.get("处置决断") or "-")),
                ("认定理由", str(sel_row.get("认定理由") or "-")),
            ])
        _render_case_query(
            result, claim_id=claim.id,
            transaction_id=sel_tx.id if picked_index >= 0 else None,
            key="review_query",
        )

    st.caption("普通候选的完整字段、处置选择和原始行号统一保留在上方审查表；需要深查时按 P 编号回到对应行。")
    # 编辑结果在本轮渲染即可见，签署前的“完成前检查”直接用它实时统计。
    edited_records = _editor_records(edited)

    # 弱信号疑似流水：付款方不是被害人本人（如亲属代付），只提示、不计入。
    weak_signals = (getattr(result, "weak_signals_by_claim", None) or {}).get(claim.id, [])
    if weak_signals:
        render_section_heading(
            "03.2 / 弱信号", "疑似关联流水（不计入金额）",
            "付款方并非被害人本人，但流水在主张期间内到达被指控收款账户；确认代付关系前一律不计入",
        )
        weak_rows = []
        for signal in weak_signals:
            tx = result.transactions[signal.transaction_id]
            weak_rows.append({
                "流水号": tx.transaction_id,
                "交易日期": str(tx.date),
                "付款人": tx.payer_name or "-",
                "付款账户ID": tx.payer_account_id or "-",
                "收款人": tx.payee_name or "-",
                "收款账户ID": tx.payee_account_id or "-",
                "金额": float(tx.amount),
                "摘要": getattr(tx, "remark", None) or "-",
                "原始证据定位": _source_locator_label(tx),
            })
        st.dataframe(
            weak_rows, width="stretch", hide_index=True,
            column_config={"金额": st.column_config.NumberColumn(format="¥ %.2f", width="small")},
        )
        st.caption(
            "以上流水不属于候选集，不进入任何金额汇总与状态判定。"
            "若经核实付款人系代被害人支付（如亲属代付），请在复核意见中记录该事实及其依据；"
            "当前版本不会据此自动计入覆盖金额。"
        )

    # 步骤三：签署复核确认
    render_section_heading("04 / 签署", "签署复核确认并保存底稿", "经办人员对事实认定与流水处置进行电子签署，签署记录入库存档、不可静默覆盖")

    # 底部再放一组与候选区一致的快捷动作，避免为处理状态回滚数屏。
    st.caption("处置状态与上方审查表共用：可在此处直接批量处理，无需回滚页面。")
    _render_candidate_batch_toolbar(candidates, state_disp_key, state_reason_key, editor_state_key, claim.id, "bottom")
    _show_batch_notice(claim.id, pop=True)

    c_r1, c_r2 = st.columns([1, 2])
    with c_r1:
        reviewer = st.text_input("复核人姓名 / 工号", value="检务复核官", key=f"reviewer_{claim.id}")
    with c_r2:
        note = st.text_input("复核意见与事实依据说明", value="经逐笔比对已登记材料与银行流水，普通候选按当前依据纳入，高风险候选保留争议并列明回查事项。", key=f"note_{claim.id}")

    # 完成前检查：实时统计当前编辑状态，把所有阻断条件在点击前直接亮出来。
    candidate_by_tid = {c.transaction_id: c for c in candidates}
    pending_rows = [row for row in edited_records if row["处置决断"] == "等待人工审查"]
    missing_reason_rows = [row for row in edited_records if not row.get("认定理由")]
    blocking_included_rows = [
        row for row in edited_records
        if row["处置决断"] == "采信纳入 (计入涉案数额)"
        and candidate_by_tid.get(row["流水号"])
        and candidate_by_tid[row["流水号"]].blocking_conflict
    ]
    reviewer_filled = bool(reviewer.strip())
    check_items = [
        (reviewer_filled, "复核人已填写" if reviewer_filled else "未填写复核人姓名 / 工号"),
        (not pending_rows, f"{len(edited_records)} 笔候选均已完成处置" if not pending_rows else f"仍有 {len(pending_rows)} 笔等待人工审查"),
        (not missing_reason_rows, "每笔候选均已选择认定理由" if not missing_reason_rows else f"{len(missing_reason_rows)} 笔缺少认定理由"),
        (not blocking_included_rows, "阻断候选均保留为争议" if not blocking_included_rows else f"{len(blocking_included_rows)} 笔高风险阻断候选被误置为采信"),
    ]
    check_html = "".join(
        f'<div style="padding:3px 0;font-size:15.5px;color:{"var(--status-ok)" if ok else "var(--status-conflict)"};">'
        f'{"✓" if ok else "!"} {html.escape(text)}</div>'
        for ok, text in check_items
    )
    st.markdown(
        f'<div style="border:1px solid var(--line);background:var(--surface);padding:12px 18px;margin:10px 0 14px;">'
        f'<div class="section-kicker" style="margin:0 0 6px;">提交前检查 · 完成状态</div>'
        f'{check_html}</div>',
        unsafe_allow_html=True,
    )
    blockers = [text for ok, text in check_items if not ok]
    submit_disabled = (not candidates) or bool(blockers)
    if blockers:
        st.caption("暂不能签署提交：" + "；".join(blockers) + "。")

    if st.button("签署复核确认，保存人工复核底稿", type="primary", disabled=submit_disabled, use_container_width=True, key=f"btn_confirm_{claim.id}"):
        dispositions = {row["_tid"]: row["处置决断"] for row in edited_records}
        if not reviewer.strip():
            st.error("必须填写复核人姓名或工号。")
        elif any(v == "等待人工审查" for v in dispositions.values()):
            st.error("仍有候选交易处于【等待人工审查】状态，请完成处置或使用【批量采纳常规候选】后再次确认。")
        elif any(not row.get("认定理由") for row in edited_records):
            st.error("每笔候选交易均须选择【认定理由】。")
        elif any(
            row["处置决断"] == "采信纳入 (计入涉案数额)"
            for row in edited_records
            if next((c for c in candidates if c.transaction_id == row["流水号"]), None)
            and next(c for c in candidates if c.transaction_id == row["流水号"]).blocking_conflict
        ):
            st.error("高风险阻断候选必须保留为【列为争议】；请先核对账户实际控制或代收关系。")
        else:
            try:
                actions = [TransactionReviewAction(
                    transaction_id=row["_tid"],
                    disposition=DISPOSITION_CN.get(row["处置决断"], "PENDING"),
                    reason_code=REASON_CN.get(row.get("认定理由"), "OTHER"),
                    note=str(row.get("经办备注") or "").strip() or None,
                ) for row in edited_records]
                decision, report = review_transactions(
                    result, actions, reviewer=reviewer.strip(),
                    claim_id=claim.id,
                    note=note.strip() or None,
                    supersedes=st.session_state.get("decision"),
                )
                st.session_state.decision = decision
                st.session_state.report = report
                repository_path = st.session_state.get("repository_path")
                checkpoint_created = not repository_path
                if repository_path:
                    saved_decision = _save_human_review(repository_path, decision, result.audit_events[-2:])
                    if saved_decision.id != decision.id or saved_decision.model_dump(mode="json") != decision.model_dump(mode="json"):
                        decision = saved_decision
                        report = build_report(
                            result.claim, decision, result.transactions,
                            claim_locators=result.claim_locators,
                            statement_conflicts=result.statement_conflicts,
                            duplicate_groups=result.duplicate_groups,
                        )
                else:
                    repository_path = _persist_result(result, audit_events=result.audit_events[:-2])
                    st.session_state.repository_path = repository_path
                    saved_decision = _save_human_review(repository_path, decision, result.audit_events[-2:])
                    if saved_decision.id != decision.id or saved_decision.model_dump(mode="json") != decision.model_dump(mode="json"):
                        decision = saved_decision
                        report = build_report(
                            result.claim, decision, result.transactions,
                            claim_locators=result.claim_locators,
                            statement_conflicts=result.statement_conflicts,
                            duplicate_groups=result.duplicate_groups,
                        )
                st.query_params["case_id"] = result.claim.case_id
                if checkpoint_created:
                    st.info("签署后的脱敏案件快照已保存到本机 SQLite；页面刷新后可自动恢复当前案件。")
                st.success(f"已保存 v{decision.version} 人工复核确认：{_status_label(decision.status.value)}。请前往【04 审查底稿留痕】查看全案底稿与导出包。")
            except Exception as exc:
                error_text = str(exc)
                if error_text == "BLOCKING_CANDIDATE_REQUIRES_DISPUTED":
                    error_text = "高风险阻断候选必须保留为争议项。"
                elif error_text.startswith("immutable decision already exists:"):
                    error_text = "该复核版本已存在且内容不同；请载入最新案件后再签署新的复核版本。"
                st.error(f"复核确认未保存：{error_text}")


def evidence_graph_page(result) -> None:
    """案件关系图：人—账户—主张—材料的关联视图，与资金流向图分工不重复。"""
    status_label = "◌ 等待人工复核" if "decision" not in st.session_state else "✓ 复核已完成"
    data_label = "实战评测卷宗" if result.claim.case_id == "GOLD_CASE_001" else "演示案件"
    render_case_masthead(result.claim.case_id, status=status_label, data_classification=data_label, review_stage="案件关系图")

    from legal_funds_agent.services.evidence_graph_service import (
        build_core_view,
        build_evidence_graph,
        evidence_graph_to_payload,
    )
    from components.evidence_graph import render_evidence_graph

    claims = result.claims if result.claims else [result.claim]
    graph = build_evidence_graph(
        claims,
        result.transactions,
        supplementary_documents=_supplementary_documents(result),
        alias_registry=getattr(result, "alias_registry", None),
    )
    stats = graph.stats()
    if not graph.nodes:
        st.info("当前案件暂无可建图的人物、账户、主张或证据材料。")
        return

    core_graph = build_core_view(
        graph,
        relevant_tx_ids={c.transaction_id for c in (result.candidates or [])} or None,
    )
    core_stats = core_graph.stats()
    render_section_heading(
        "01 / 关系图",
        "案件关系图",
        f"核心视图 {core_stats.get('nodes', 0)} 个节点 · {core_stats.get('edges', 0)} 条关系"
        f"（完整关联 {stats.get('nodes', 0)} 节点 · {stats.get('edges', 0)} 条）",
    )
    st.caption(
        "核心视图只保留办案关键要素：被害人、嫌疑人、第三方收款人、起诉书载明账户、付款主张，"
        "以及与指控候选流水相关的资金往来，让办案人员一眼看清案件骨架；与指控无关的流水、证据材料提及、"
        "别名记录等细节默认收起，可用下方开关展开完整关联。"
        "金色虚线为待证/争议关系（如第三方代收），一律不构成确定事实；所有节点与关系均可回溯到原始流水行、主张定位或材料文件。"
        "“钱怎么走”请看【02 涉案资金流水】页的资金流向图。"
    )
    show_full = st.toggle(
        "展开完整关联（含证据材料提及、别名记录等辅助细节）",
        value=False,
        key="evidence_graph_show_full",
    )
    active_graph = graph if show_full else core_graph
    if not active_graph.nodes:
        st.info("核心视图暂无可展示内容，可展开完整关联查看。")
        return

    payload = evidence_graph_to_payload(active_graph)
    sel_state_key = "evidence_graph_main__selection_echo"
    last_selection = st.session_state.get(sel_state_key)
    if last_selection:
        payload["selected"] = last_selection

    # 左图右 Inspector：点击节点/关系后，来源回溯固定在右侧栏，不用滚屏。
    col_graph, col_inspector = st.columns([2.2, 1])
    with col_graph:
        state = render_evidence_graph(payload, height=560, key="evidence_graph_main")

    selection = None
    if state is not None:
        selection = getattr(state, "selection", None)
        if selection is None and isinstance(state, dict):
            selection = state.get("selection")
    current_selection = (
        {"type": selection.get("type"), "id": selection.get("id")} if selection else None
    )
    if current_selection != last_selection:
        st.session_state[sel_state_key] = current_selection

    with col_inspector:
        if not selection:
            st.caption("点击图中人物、账户、主张或关系线，此处显示其来源回溯；滚轮缩放、拖拽平移。")
            _render_case_query(result, key="graph_query")
            return

        lookup: dict[tuple[str, str], dict] = {}
        for node in payload.get("nodes", []):
            lookup[("node", node.get("id"))] = node
        for edge in payload.get("edges", []):
            lookup[("edge", edge.get("id"))] = edge
        entry = lookup.get((selection.get("type"), selection.get("id"))) or selection

        def _ref_text(ref: dict) -> str:
            return " · ".join(f"{k}={v}" for k, v in ref.items() if v not in (None, "", []))

        if selection.get("type") == "node":
            items = [
                ("节点类型", entry.get("role_label") or entry.get("type", "-")),
                ("脱敏账号", entry.get("masked_account") or "-"),
                ("主张金额", f"¥{entry.get('amount'):,.2f}" if entry.get("amount") else "-"),
            ]
            refs = entry.get("source_refs") or []
            for idx, ref in enumerate(refs[:8], 1):
                items.append((f"来源 {idx:02d}", _ref_text(ref)))
            if len(refs) > 8:
                items.append(("…", f"另有 {len(refs) - 8} 条来源记录"))
            _evidence_card(f"节点 · {entry.get('label') or entry.get('name', '-')}", items)
        else:
            items = [
                ("关系类型", entry.get("type", "-")),
                ("关系性质", "! 待证/争议（不构成确定事实）" if entry.get("disputed") else "✓ 由确定性记录直接得出"),
                ("累计金额", f"¥{entry.get('amount'):,.2f}" if entry.get("amount") else "-"),
                ("流水笔数", f"{entry.get('count', 1)} 笔"),
                ("说明", entry.get("reason") or "-"),
            ]
            refs = entry.get("source_refs") or []
            for idx, ref in enumerate(refs[:8], 1):
                items.append((f"来源 {idx:02d}", _ref_text(ref)))
            if len(refs) > 8:
                items.append(("…", f"另有 {len(refs) - 8} 条来源记录"))
            _evidence_card("关系 · 待人工核验" if entry.get("disputed") else "关系 · 记录确认", items)
        query_refs = entry.get("source_refs") or []
        selected_claim_id = next((ref.get("claim_id") for ref in query_refs if ref.get("claim_id")), None)
        selected_tx_ids = {ref["transaction_id"] for ref in query_refs if ref.get("transaction_id")}
        selected_tx_id = next(iter(selected_tx_ids)) if len(selected_tx_ids) == 1 else None
        _render_case_query(
            result, claim_id=selected_claim_id, transaction_id=selected_tx_id,
            entity=entry.get("name") if selection.get("type") == "node" else None,
            key="graph_query",
        )


def audit_page(result) -> None:
    status_label = "✓ 复核已完成" if st.session_state.get("decision") else "◌ 等待人工复核"
    data_label = "实战评测卷宗" if result.claim.case_id == "GOLD_CASE_001" else "演示案件"
    render_case_masthead(result.claim.case_id, status=status_label, data_classification=data_label, review_stage="审查底稿与审计留痕")

    render_section_heading("01 / 审计留痕", "全案审计留痕日志", "电子证据链完整性与防静默篡改留痕")

    step_map = {
        "claim_extraction": "起诉书事实主张提取",
        "statement_comparison": "被害人陈述交叉核验",
        "transaction_parser": "银行流水解析规范化",
        "candidate_matcher": "资金穿透智能比对",
        "human_review": "人工复核确认",
    }
    tool_map = {
        "regex_provider": "本地语义规则引擎",
        "deepseek_provider": "DeepSeek 语义提取",
        "transaction_parser": "银行流水规范化解析器",
        "candidate_matcher": "穿透对账算法引擎",
        "human_reviewer": "经办人人工复核控制台",
    }
    audit_rows = []
    for idx, event in enumerate(result.audit_events, 1):
        audit_rows.append({
            "序号": idx,
            "审查阶段": step_map.get(event.step, event.step),
            "调用工具": tool_map.get(event.tool, event.tool),
            "执行状态": "✓ 成功完成" if event.status == "success" else "! 异常中断",
            "阶段耗时": f"{event.duration_ms} 毫秒",
            "模型引擎": event.model or "本地规则引擎",
            "输入 Tokens": event.input_tokens or "-",
            "输出 Tokens": event.output_tokens or "-",
            "记录时间": (event.finished_at or "-")[:19].replace("T", " "),
            "防伪数据哈希": (event.output_hash or "-")[:16] + "..." if event.output_hash else "-",
        })
    st.dataframe(audit_rows, width="stretch", hide_index=True)

    report = st.session_state.get("report")
    decision = st.session_state.get("decision")
    if not report or not decision:
        st.info("完成涉案主张人工复核后可查看和导出全案复核底稿。")
        return

    render_section_heading("02 / 复核结论", "当前主张复核认定结果", "经办人员人工签署意见与事实覆盖")
    render_stat_strip([
        ("人工复核状态", _status_label(decision.status.value), "经办人复核认定"),
        ("资金证据覆盖", f"¥{decision.covered_amount:,.2f}", "银行流水证实"),
        ("未覆盖差额", f"¥{decision.uncovered_amount:,.2f}", "尚待查明差额"),
        ("复核版本", f"v{decision.version}", "防静默篡改底稿编号"),
    ])
    render_review_summary("资金证据核验结论", f"当前材料中，已人工纳入流水 {len(decision.included_transaction_ids)} 笔，共计人民币 ¥{decision.covered_amount:,.2f}；尚未覆盖 ¥{decision.uncovered_amount:,.2f}。复核人：{decision.reviewer or '未填写'}。")
    st.markdown('<div class="legal-notice"><strong>法律效力提示：</strong>' + report["disclaimer"] + '</div>', unsafe_allow_html=True)
    json_text = report_to_json(report)
    csv_text = report_to_csv(report)
    html_text = report_to_html(report)

    from legal_funds_agent.services.case_report_service import build_case_master_report, case_report_to_html
    claims_list = result.claims if getattr(result, "claims", None) else [result.claim]
    conflict_matrix = _conflict_matrix_for(result, _supplementary_documents(result))
    decisions_dict = {}
    if getattr(result, "system_decisions_by_claim", None):
        decisions_dict = dict(result.system_decisions_by_claim)
    if decision:
        decisions_dict[decision.claim_id] = decision
    elif not decisions_dict and result.system_decision:
        decisions_dict[result.claim.id] = result.system_decision

    claim_audit_result = getattr(result, "claim_audit", None)
    master_rep = build_case_master_report(
        case_id=result.claim.case_id,
        claims=claims_list,
        decisions_by_claim=decisions_dict,
        transactions=result.transactions,
        audit_events=result.audit_events,
        claim_locators=result.claim_locators,
        evidence_conflicts=conflict_matrix,
        extraction_issues=getattr(result, "extraction_issues", []),
        missing_claims=getattr(claim_audit_result, "missing_claims", []) if claim_audit_result else [],
        alias_registry=getattr(result, "alias_registry", None),
    )
    master_summary = master_rep.get("summary", {})
    ref_amt = master_summary.get("total_refund_amount", 0.0)
    net_amt = master_summary.get("net_claimed_amount", master_summary.get("total_claimed_amount", 0.0))

    render_section_heading("03 / 全案底稿", "全案资金证据核验底稿", "起诉主张、全案流水与事实交叉比对汇总")
    render_stat_strip([
        ("指控涉案总额", f"¥{master_summary.get('total_claimed_amount', 0.0):,.2f}", "起诉书指控事实"),
        ("疑似转回流水", f"¥{ref_amt:,.2f}", "案发前转回待查"),
        ("未返还参考", f"¥{net_amt:,.2f}", "数学差额参考"),
        ("已确证覆盖", f"¥{master_summary.get('total_covered_amount', 0.0):,.2f}", "银行流水穿透"),
    ])

    if ref_amt > 0:
        st.info(f"【疑似转回流水核对】按账户关系和唯一交易事件识别到 {len(identify_refund_transactions(claims_list, result.transactions.values()))} 笔、¥{ref_amt:,.2f} 元可能转入被害人账户；摘要不能单独证明返还性质或法定冲减效果，¥{net_amt:,.2f} 仅为参考值。")

    st.caption(f"【审查结果数据完整性指纹 (SHA-256)】：`{master_rep['data_integrity_sha256']}`")

    extraction_issues = getattr(result, "extraction_issues", [])
    claim_audit = getattr(result, "claim_audit", None)
    statement_warnings = getattr(result, "statement_extraction_warnings", [])
    audit_queue = getattr(claim_audit, "missing_claims", []) if claim_audit else []
    if extraction_issues or audit_queue or statement_warnings:
        with st.expander("提取质量与漏提复核", expanded=bool(extraction_issues or audit_queue)):
            st.caption(
                "以下为提取质量信号，不参与金额、覆盖与状态判定；未经人工确认前不会进入资金复核。"
            )
            for item in extraction_issues:
                st.warning(
                    f"主张 {item['claim_id']}（¥{item['claimed_amount']}）："
                    f"{'、'.join(item['issues'])}。{item['next_action']}"
                )
            for item in audit_queue:
                suffix = f"｜{item['anchor_issue']}" if item.get("anchor_issue") else ""
                st.info(
                    f"疑似漏项 {item['pending_id']}（{item['status']}）："
                    f"{item['victim_name']} ¥{item['claimed_amount']}{suffix}"
                    f"\n\n原文：{item['source_text']}"
                )
            if statement_warnings:
                st.caption("陈述提取降级记录：" + "；".join(statement_warnings))

    with st.expander("查看补充调查与原始材料回查清单", expanded=True):
        st.caption("每项建议都保留关联流水或主张的原始定位；系统不改写原始 Word/Excel，只提供回查入口和执行记录。")
        checklist = master_rep.get("investigation_checklist", [])
        stored_items = _load_investigation_items(
            st.session_state.get("repository_path"), result.claim.case_id
        )
        stored_statuses = {item.get("item_id"): item.get("status") for item in stored_items}
        for item in checklist:
            if item.get("item_id") in stored_statuses:
                item["status"] = stored_statuses[item["item_id"]]

        # Reword before the items are drawn: the reviewer should read the same text that
        # the exported workbook contains, not the pre-enhancement template.
        if _model_enhancement_enabled():
            checklist = _apply_model_wording(checklist, provider_name)

        checklist = _apply_checklist_statuses(result.claim.case_id, checklist)
        _save_investigation_items(st.session_state.get("repository_path"), result.claim.case_id, checklist)
        master_rep["investigation_checklist"] = checklist
        pending_count = sum(item.get("status") != "已核查" for item in checklist)
        st.markdown(
            f'<div class="review-summary">'
            f'<div class="section-kicker">后续核查 · 重点回查</div>'
            f'<strong>待核查补充侦查建议项</strong>'
            f'<div class="stat-value">{pending_count:02d} 项</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    if _model_enhancement_enabled():
        from legal_funds_agent.services.case_narrative_service import (
            build_narrative_facts,
            narrative_body_html,
        )

        narrative_outcome = _cached_narrative(
            _json_arg(build_narrative_facts(master_rep)), provider_name
        )
        if narrative_outcome.get("narrative"):
            master_rep["narrative"] = narrative_outcome["narrative"]
            st.caption("模型增强：全案审查意见摘要已生成，随底稿 HTML 导出。")
            render_section_heading("05 / 审查摘要", "全案审查意见摘要", "模型在确定性事实之上的转写，不构成法律结论")
            st.markdown(narrative_body_html(master_rep["narrative"]), unsafe_allow_html=True)
        else:
            audit = narrative_outcome.get("audit") or {}
            reason = audit.get("rejected") or "；".join(audit.get("notes") or []) or "模型未返回摘要"
            st.caption(f"模型增强：未生成全案摘要（{reason}）。底稿仍以表格形式完整导出。")

    master_html = case_report_to_html(master_rep)
    master_json = case_report_to_json(master_rep)
    checklist_json = json.dumps(checklist, ensure_ascii=False, indent=2)
    checklist_csv = _checklist_csv(checklist)

    render_section_heading("04 / 导出", "复核底稿与数据溯源包导出", "HTML / PDF / JSON / CSV 留痕归档")
    st.caption("底稿包含重点资金流向、疑似转回流水、证据冲突焦点、原始行号和回查清单；不修改原始 Word/Excel，打开 HTML 后可打印或保存为 PDF。")

    col_btn1, col_btn2 = st.columns([1.3, 1])
    col_btn1.download_button(
        "导出全案资金核验底稿（HTML · 可打印或另存为 PDF）",
        master_html,
        file_name=f"{result.claim.case_id}-资金证据核验底稿.html",
        mime="text/html",
        width="stretch",
        type="primary",
    )
    col_btn2.download_button(
        "导出当前主张复核底稿（HTML）",
        html_text,
        file_name=f"{result.claim.case_id}-{result.claim.id}-审查底稿.html",
        mime="text/html",
        width="stretch",
    )

    with st.expander("审查数据溯源包（JSON / CSV 复核备查）", expanded=False):
        st.caption("导出脱敏后的金额、交易、来源行号、处置记录、冲突焦点和数据完整性指纹，便于复核备查。")
        col_f1, col_f2 = st.columns(2)
        col_f1.download_button("下载全案主数据溯源包 (JSON)", master_json, file_name=f"{result.claim.case_id}-master-data.json", mime="application/json", width="stretch")
        col_f2.download_button("下载已采信对账流水 (CSV)", "\ufeff" + csv_text, file_name=f"{result.claim.case_id}-transactions.csv", mime="text/csv", width="stretch")
        col_f3, col_f4 = st.columns(2)
        col_f3.download_button("下载调查回查清单 (JSON)", checklist_json, file_name=f"{result.claim.case_id}-investigation-checklist.json", mime="application/json", width="stretch")
        col_f4.download_button("下载调查回查清单 (CSV)", "\ufeff" + checklist_csv, file_name=f"{result.claim.case_id}-investigation-checklist.csv", mime="text/csv", width="stretch")


if st.session_state.get("result") is None:
    restored_case_id = st.query_params.get("case_id")
    database_path = ROOT / "data" / "cases.db"
    if restored_case_id and database_path.exists():
        _restore_case_into_session(database_path, restored_case_id)

st.sidebar.markdown("""
<div class="sidebar-brand-block">
  <div><span class="sidebar-brand-badge"></span><span class="sidebar-brand-name">资金链证审</span></div>
  <div class="sidebar-brand-sub">资金证据审查系统</div>
</div>
""", unsafe_allow_html=True)

PAGES = [
    "01  案件审查概览",
    "02  涉案资金流水",
    "03  资金证据核验",
    "04  审查底稿留痕",
    "05  案件关系图",
]

# 侧边栏信息层级：当前案件 → 工作区导航（主角）→ 案件管理 → 设置（沉底）。
sidebar_result = st.session_state.get("result")
case_display_names = _load_case_display_names()
sidebar_notice = st.session_state.pop("sidebar_notice", None)
if sidebar_notice:
    st.sidebar.success(sidebar_notice)
st.sidebar.markdown('<div class="section-kicker" style="margin-top:8px;">当前案件</div>', unsafe_allow_html=True)
if sidebar_result is None:
    st.sidebar.markdown('<span class="accessible-status status-insufficient"><span class="status-symbol">—</span> 尚未加载案件</span>', unsafe_allow_html=True)
else:
    signed = "decision" in st.session_state
    status_cls = "status-ok" if signed else "status-pending"
    status_sym = "✓" if signed else "◌"
    status_txt = "复核已签署完成" if signed else "等待人工复核"
    st.sidebar.markdown(
        f'<span class="accessible-status {status_cls}"><span class="status-symbol">{status_sym}</span> {status_txt}</span>',
        unsafe_allow_html=True,
    )
    active_case_id = sidebar_result.claim.case_id
    active_display = case_display_names.get(active_case_id)
    st.sidebar.caption(f"案件：{active_display}（{active_case_id}）" if active_display else f"案件编号：{active_case_id}")
    if signed:
        st.sidebar.caption(f"底稿版本 v{st.session_state.decision.version}")
    with st.sidebar.popover("重命名当前案件", use_container_width=True):
        new_name = st.text_input(
            "案件显示名（仅展示用途，案件编号不变）",
            value=case_display_names.get(active_case_id, ""),
            key="sidebar_rename_input",
        )
        if st.button("保存显示名", key="sidebar_rename_save", type="primary"):
            with closing(connect(ROOT / "data" / "cases.db")) as connection:
                Repository(connection).save_case_display_name(
                    active_case_id, new_name.strip() or active_case_id
                )
            st.rerun()

st.sidebar.markdown('<div class="section-kicker sidebar-operation-heading">工作区导航</div>', unsafe_allow_html=True)
# Use explicit buttons rather than a visually collapsed radio control. Each
# button writes the target page before rerunning, so a click has an immediate,
# inspectable destination even when the current page contains a large editor.
# 当前页用 primary 渲染，再由 CSS 压平为“左侧藏青竖条”选中态，不再叠加字符标记。
if st.session_state.get("nav_page") not in PAGES:
    st.session_state["nav_page"] = PAGES[0]
page_selection = st.session_state["nav_page"]
for nav_index, nav_label in enumerate(PAGES, 1):
    if st.sidebar.button(
        nav_label,
        key=f"sidebar_nav_{nav_index}",
        use_container_width=True,
        type="primary" if page_selection == nav_label else "secondary",
    ):
        st.session_state["nav_page"] = nav_label
        st.rerun()

st.sidebar.divider()
st.sidebar.markdown('<div class="section-kicker sidebar-operation-heading">案件管理</div>', unsafe_allow_html=True)
if st.sidebar.button("新建案件", use_container_width=True, key="sidebar_new_case"):
    # New means a clean workbench, not merely jumping to the old materials
    # panel. Persisted SQLite checkpoints remain untouched until the user
    # explicitly deletes a history entry below.
    for state_key in (
        "result", "decision", "report", "repository_path", "supplementary_documents",
        "failed_audit_events", "materials_panel_expanded",
    ):
        st.session_state.pop(state_key, None)
    st.session_state["nav_page"] = PAGES[0]
    st.session_state["materials_panel_expanded"] = True
    st.session_state["material_source"] = "上传材料"
    st.session_state["landing_panel"] = True
    st.query_params.pop("case_id", None)
    st.session_state["sidebar_notice"] = "已进入新建案件工作区；历史案件记录未删除。"
    st.rerun()

# 历史案件：加载与删除放在同一分区，避免把“新建”和“切换”误解为同一个动作。
_history_db = ROOT / "data" / "cases.db"
if _history_db.exists():
    with closing(connect(_history_db)) as connection:
        history_cases = Repository(connection).list_cases()
else:
    history_cases = []
if history_cases:
    switch_options = {
        (
            f"{case_display_names[c['case_id']]}（{c['case_id']}）"
            if case_display_names.get(c['case_id'])
            else f"{c['case_id']}（主张 {c['claim_count']} · 流水 {c['tx_count']}）"
        ): c["case_id"]
        for c in history_cases
    }
    selected_label = st.sidebar.selectbox("打开历史案件", list(switch_options.keys()), key="sidebar_history_case")
    if st.sidebar.button("打开所选案件", use_container_width=True, key="sidebar_load_case"):
        target_case_id = switch_options[selected_label]
        if _restore_case_into_session(_history_db, target_case_id):
            st.session_state["nav_page"] = PAGES[0]
            st.query_params["case_id"] = target_case_id
            st.session_state["sidebar_notice"] = f"已打开历史案件：{selected_label}"
            st.rerun()
        else:
            st.sidebar.error("该案件未读取到有效主张或流水数据。")

    with st.sidebar.popover("删除历史案件", use_container_width=True):
        delete_label = st.selectbox("选择要删除的案件", list(switch_options.keys()), key="sidebar_delete_case")
        st.warning("删除只会移除本机 SQLite 中的脱敏案件快照、复核记录和审计记录，不会删除原始 Word/Excel 文件。")
        confirm_delete = st.checkbox("我确认永久删除所选历史案件", key="sidebar_delete_confirm")
        if st.button("确认删除", type="secondary", use_container_width=True, key="sidebar_delete_submit"):
            if not confirm_delete:
                st.error("请先勾选确认框。")
            else:
                target_case_id = switch_options[delete_label]
                with closing(connect(_history_db)) as connection:
                    Repository(connection).delete_case(target_case_id)
                if st.session_state.get("result") is not None and st.session_state.result.claim.case_id == target_case_id:
                    for state_key in (
                        "result", "decision", "report", "repository_path", "supplementary_documents",
                        "failed_audit_events", "materials_panel_expanded",
                    ):
                        st.session_state.pop(state_key, None)
                    st.session_state["nav_page"] = PAGES[0]
                    st.query_params.pop("case_id", None)
                st.session_state["sidebar_notice"] = f"已删除历史案件：{delete_label}"
                st.rerun()
else:
    st.sidebar.caption("暂无已保存的历史案件。")

st.sidebar.divider()
# 设置沉底：办案人员打开系统不应首先面对模型配置。
with st.sidebar.expander("⚙ 模型与规则配置", expanded=False):
    # URL overrides let a demo link or an automated check open the workbench already
    # configured, e.g. ?case_id=CASE-0001&provider=deepseek&enhance=1
    requested_provider = st.query_params.get("provider")
    if requested_provider in PROVIDER_CHOICES:
        st.session_state.setdefault("provider_name", requested_provider)
    if str(st.query_params.get("enhance", "")).strip().lower() in {"1", "true", "yes"}:
        st.session_state.setdefault("model_enhancement_enabled", True)

    provider_name = st.selectbox(
        "模型服务",
        PROVIDER_CHOICES,
        key="provider_name",
        format_func=_provider_label,
        help="Mock 不联网；DeepSeek 负责事实主张提取，金额穿透与审查状态始终由确定性规则完成。"
    )
    enable_claim_audit = st.checkbox(
        "启用漏提复核",
        value=False,
        help="对同一份起诉书做第二次对抗式提取。疑似漏项只作为待人工确认事项，不会自动并入资金复核。",
    )
    model_enhancement = st.checkbox(
        "启用模型增强",
        key="model_enhancement_enabled",
        help=(
            "让模型为冲突比对补充材料引文、为回查建议改写措辞、生成全案摘要。"
            "数字一律由确定性代码提供，模型不得新增；结果按内容缓存，同一案件不会重复计费。"
        ),
    )

if sidebar_result is not None:
    claim_events = [e for e in getattr(sidebar_result, "audit_events", []) if getattr(e, "step", None) == "claim_extraction"]
    if claim_events and getattr(claim_events[-1], "input_tokens", None) is not None and claim_events[-1].input_tokens > 0:
        ce = claim_events[-1]
        with st.sidebar.expander("大模型诊断指标", expanded=False):
            st.write(f"模型耗时 · {ce.latency_ms or 0} ms")
            st.write(f"输入 Tokens · {ce.input_tokens}")
            st.write(f"输出 Tokens · {ce.output_tokens or 0}")

if page_selection.startswith("01"):
    case_page()
elif sidebar_result is None:
    render_section_heading("页面 / 空", "请先加载案卷材料", "当前工作区需要有效案件数据")
    st.warning("— 当前没有可用审查任务。请前往【01  案件审查概览】载入案卷或上传材料。")
elif page_selection.startswith("02"):
    transactions_page(sidebar_result)
elif page_selection.startswith("03"):
    review_page(sidebar_result)
elif page_selection.startswith("05"):
    evidence_graph_page(sidebar_result)
else:
    audit_page(sidebar_result)
