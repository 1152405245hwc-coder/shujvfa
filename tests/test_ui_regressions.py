from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "ui" / "streamlit_app.py"
QUERY_PANEL_PATH = ROOT / "ui" / "case_query_panel.py"
COMPONENT_PATH = ROOT / "ui" / "components" / "fund_flow" / "component.py"
RENDERER_PATH = ROOT / "ui" / "components" / "fund_flow" / "assets" / "renderer.js"
CSS_PATH = ROOT / "ui" / "components" / "fund_flow" / "assets" / "component.css"


class LandingButtonsRegressionTest(unittest.TestCase):
    def test_panel_opening_buttons_clear_restored_case_query_param(self):
        source = APP_PATH.read_text(encoding="utf-8")
        for key in ("landing_new", "landing_history"):
            marker = f'key="{key}"'
            pos = source.index(marker)
            following = source[pos:pos + 360]
            self.assertIn(
                'st.query_params.pop("case_id", None)',
                following,
                f"{key} must clear a stale ?case_id before rerun",
            )

    def test_upload_button_scrolls_directly_to_materials_intake(self):
        source = APP_PATH.read_text(encoding="utf-8")
        pos = source.index('key="landing_new"')
        following = source[pos:pos + 420]
        self.assertIn('st.session_state["scroll_to_materials_intake"] = True', following)
        self.assertIn('id="materials-intake-anchor"', source)
        self.assertIn("scrollIntoView", source)

    def test_only_complex_demo_case_is_exposed(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertNotIn('key="landing_demo_d01"', source)
        self.assertNotIn('source_options = ["演示案例"', source)
        self.assertNotIn('if source == "演示案例"', source)
        self.assertIn('打开演示案件（736.8万）', source)
        gold_pos = source.index('key="landing_demo_gold"')
        self.assertIn("_run_gold_case(", source[gold_pos:gold_pos + 420])
        self.assertIn("provider_name=provider_name", source[gold_pos:gold_pos + 420])


class FundFlowInteractionRegressionTest(unittest.TestCase):
    def test_component_does_not_register_a_selection_rerun_callback(self):
        source = COMPONENT_PATH.read_text(encoding="utf-8")
        mount_call = source[source.index("    return fund_flow("):]
        self.assertNotIn("on_selection_change", mount_call)
        self.assertNotIn('default={"selection"', mount_call)

    def test_renderer_keeps_node_clicks_in_the_browser(self):
        source = RENDERER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("setStateValue", source)
        self.assertNotIn("payload.selected", source)
        self.assertIn("evt.target.select();", source)

    def test_fullscreen_overlay_has_opaque_rebuild_background(self):
        source = CSS_PATH.read_text(encoding="utf-8")
        self.assertIn(".ff-root.ff-expanded::before", source)
        self.assertIn("z-index: 999999", source)


class CaseGuideRegressionTest(unittest.TestCase):
    def test_guide_page_is_registered_for_cold_start_reviewers(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('PAGE_GUIDE = "00  案件导读（评审）"', source)
        self.assertIn("def case_guide_page(result)", source)
        self.assertIn('page_selection.startswith("00")', source)
        self.assertIn("case_guide_page(sidebar_result)", source)

    def test_gold_case_guide_contains_the_five_review_questions(self):
        source = APP_PATH.read_text(encoding="utf-8")
        for text in (
            "本案到底要核验什么",
            "736.8 万元是否能够被银行流水完整印证？",
            "林某账户究竟是什么性质？",
            "132.6 万元是真实投资收益、返款，还是需要进一步核验的疑似转回？",
            "90 秒口头讲解稿（供路演使用）",
        ):
            self.assertIn(text, source)


class GuidedDemoRegressionTest(unittest.TestCase):
    def test_gold_guide_is_an_interactive_five_step_demo(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _render_gold_guided_demo(guide)", source)
        self.assertIn('"五步引导演示"', source)
        self.assertIn("_render_gold_guided_demo(guide)", source)
        self.assertIn('key="gold_demo_next"', source)
        self.assertIn('key="gold_demo_view"', source)
        self.assertIn("gold_demo_step", source)

    def test_guide_metrics_come_from_deterministic_results(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("identify_refund_transactions(claims_list, result.transactions.values())", source)
        self.assertIn('("第三方代收", f"¥{third_party_total:,.2f}"', source)

    def test_page_constants_are_defined_once(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count('PAGE_GUIDE = "00  案件导读（评审）"'), 1)
        self.assertEqual(source.count("PAGES = ["), 1)

    def test_fund_use_summary_is_wired_into_transactions_page(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _render_fund_use_summary(result, claims, refund_txs)", source)
        self.assertIn("_render_fund_use_summary(result, claims, refund_txs)", source)
        self.assertIn("资金实际用途核验摘要", source)
        self.assertIn("build_fund_use_summary", source)

    def test_demo_context_bar_appears_on_evidence_pages(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _render_demo_context_bar()", source)
        self.assertGreaterEqual(source.count("_render_demo_context_bar()"), 5)
        self.assertIn("gold_demo_return_step", source)
        self.assertIn("返回导读", source)

    def test_landing_copy_states_the_showcase_value(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "90 秒体验：系统如何从 736.8 万元复杂流水中识别第三方代收、重复流水、资金去向与证据冲突。",
            source,
        )


class DeepSeekKeyConfigTest(unittest.TestCase):
    def test_session_key_entry_is_available_in_the_sidebar(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('key="deepseek_api_key_input"', source)
        self.assertIn('type="password"', source)
        self.assertIn('key="test_deepseek_connection"', source)
        self.assertIn("verify_connection()", source)

    def test_key_waits_for_explicit_test_click(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("@st.fragment\ndef _render_deepseek_key_config")
        end = source.index("# Truthy spellings accepted", start)
        config = source[start:end]
        self.assertNotIn("Auto-verify", config)
        self.assertIn('test_clicked = st.button(', config)
        self.assertIn('if test_clicked:', config)
        self.assertEqual(config.count("_run_deepseek_connection_check(effective_key)"), 1)
        self.assertNotIn("点击【测试连接】后才会发起校验", config)

    def test_empty_key_click_shows_validation_without_disabling_button(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("@st.fragment\ndef _render_deepseek_key_config")
        end = source.index("# Truthy spellings accepted", start)
        config = source[start:end]
        self.assertNotIn("disabled=not bool(effective_key)", config)
        self.assertIn("if not effective_key:", config)
        self.assertIn('connection_result.warning("请先输入 DeepSeek 接口密钥。")', config)

    def test_streamlit_stop_widget_is_hidden_in_favor_of_local_status(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('[data-testid="stStatusWidget"]', source)

    def test_new_key_clears_previous_connection_result(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("@st.fragment\ndef _render_deepseek_key_config")
        end = source.index("# Truthy spellings accepted", start)
        config = source[start:end]
        self.assertIn('previous_candidate != effective_key', config)
        self.assertIn('st.session_state.pop("deepseek_key_status", None)', config)
        self.assertIn('connection_result = st.empty()', config)
        self.assertIn('with connection_result.container():', config)

    def test_connection_check_uses_local_fragment_and_compact_status(self):
        source = APP_PATH.read_text(encoding="utf-8")
        fragment_pos = source.index("@st.fragment\ndef _render_deepseek_key_config")
        config_call_pos = source.index("        _render_deepseek_key_config()")
        self.assertLess(fragment_pos, config_call_pos)
        self.assertIn('st.status("正在测试 DeepSeek 连接……", expanded=False)', source)
        self.assertNotIn('key="retest_deepseek_connection"', source)

    def test_clear_key_uses_pre_rerun_callback_and_resets_widget_value(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def _clear_deepseek_key_config()")
        end = source.index("@st.fragment", start)
        clear_callback = source[start:end]
        self.assertIn('st.session_state["deepseek_api_key_input"] = ""', clear_callback)
        self.assertIn('st.session_state.pop("deepseek_key_candidate", None)', clear_callback)
        self.assertIn('st.session_state.pop("deepseek_key_status", None)', clear_callback)
        self.assertIn('on_click=_clear_deepseek_key_config', source)

    def test_container_injected_key_stays_as_fallback(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('DEEPSEEK_ENV_KEY = os.environ.get("DEEPSEEK_API_KEY", "")', source)
        self.assertIn('page_key or DEEPSEEK_ENV_KEY', source)
        self.assertIn('"运行环境变量" if DEEPSEEK_ENV_KEY', source)

    def test_page_key_is_not_written_to_process_environment(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertNotIn('os.environ["DEEPSEEK_API_KEY"] = page_key', source)
        self.assertIn(
            'provider_from_environment("deepseek", api_key=page_key or DEEPSEEK_ENV_KEY)',
            source,
        )

    def test_missing_key_error_is_actionable(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('if "DEEPSEEK_API_KEY" in str(exc):', source)
        self.assertIn("请展开左侧【⚙ 模型与规则配置】粘贴密钥", source)
        self.assertIn("或切换回【本地模拟（推荐演示）】", source)

    def test_intelligent_query_uses_the_same_page_provider_config(self):
        app_source = APP_PATH.read_text(encoding="utf-8")
        panel_source = QUERY_PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("provider_factory=lambda: _provider_from_ui_config(provider_name)", app_source)
        self.assertIn("provider_factory()", panel_source)
        self.assertIn('provider_from_environment(provider_name)', panel_source)

    def test_model_cache_key_contains_non_secret_provider_fingerprint(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _provider_cache_context(provider_name: str):", source)
        self.assertIn('"credential_hash": hashlib.sha256(effective_key.encode("utf-8")).hexdigest()', source)
        self.assertIn("provider_fingerprint: str, _provider", source)
        self.assertIn("raise_on_provider_error=True", source)


class HtmlEscapingRegressionTest(unittest.TestCase):
    def test_dynamic_claim_fields_are_escaped_before_unsafe_html_rendering(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("html.escape(str(claim.victim_name))", source)
        self.assertIn("html.escape(str(claim.time_start))", source)
        self.assertIn('html.escape(str(claim.alleged_recipient_name or "指定涉案账户"))', source)

    def test_shared_heading_escapes_all_dynamic_text(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def render_section_heading(")
        end = source.index("STATUS_CONFIG =", start)
        helper = source[start:end]
        self.assertIn("html.escape(str(subtitle))", helper)
        self.assertIn("html.escape(str(number))", helper)
        self.assertIn("html.escape(str(title))", helper)

    def test_multi_claim_banner_escapes_material_derived_labels(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('progress_detail = html.escape("；".join(parts))', source)
        self.assertIn("conflict_labels = html.escape(", source)


class EvidenceGraphLayoutRegressionTest(unittest.TestCase):
    def test_graph_detail_and_query_use_stacked_full_width_sections(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def evidence_graph_page(result)")
        end = source.index("def audit_page(result)", start)
        graph_page = source[start:end]

        self.assertNotIn("st.columns([2.2, 1])", graph_page)
        graph_pos = graph_page.index("state = render_evidence_graph(")
        detail_pos = graph_page.index('"02 / 关系详情"')
        query_pos = graph_page.index('"03 / 智能查询"')
        self.assertLess(graph_pos, detail_pos)
        self.assertLess(detail_pos, query_pos)
        self.assertIn("show_heading=False", graph_page)

    def test_query_panel_can_hide_its_heading_inside_a_named_page_section(self):
        source = QUERY_PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("show_heading=True", source)
        self.assertIn("if show_heading:", source)


class ReviewFlowRegressionTest(unittest.TestCase):
    def test_saved_review_report_and_filename_use_the_current_claim(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("report = build_claim_report(result, claim, decision)"), 2)
        self.assertIn("st.session_state.report = report", source)
        self.assertIn(
            'file_name=f"{decision.case_id}-{decision.claim_id}-审查底稿.html"',
            source,
        )
        self.assertNotIn(
            'file_name=f"{result.claim.case_id}-{result.claim.id}-审查底稿.html"',
            source,
        )

    def test_supersedes_is_scoped_to_the_current_claim(self):
        """多主张案件中，会话里保存的可能是另一主张的已签署决定；跨主张直接作为
        supersedes 传入会触发 SUPERSEDES_CLAIM_MISMATCH，签署路径必须按主张过滤。"""
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("supersedes = claim_context.latest_human_decision", source)
        self.assertNotIn("supersedes=st.session_state.get(\"decision\")", source)
        self.assertIn("复核版本链与当前主张不一致", source)

    def test_multi_claim_progress_banner_is_shown_on_review_and_audit_pages(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _render_multi_claim_progress(result, claims_list, decisions_map=None)", source)
        self.assertIn("多主张案件进度提醒", source)
        self.assertIn("HUMAN_CONFIRMED", source)
        self.assertEqual(source.count("_render_multi_claim_progress("), 3)

    def test_review_page_uses_one_claim_scoped_context(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def review_page(result)")
        end = source.index("def evidence_graph_page(result)", start)
        review_page = source[start:end]

        self.assertIn("claim_context = build_claim_review_context(result, claim, decisions_map)", review_page)
        self.assertIn("claim_context.latest_human_decision", review_page)
        self.assertIn("claim_context.statement_conflicts", review_page)
        self.assertIn("claim_context.source_locators", review_page)
        self.assertNotIn("result.statement_conflicts", review_page)
        self.assertNotIn("result.claim_locators", review_page)

    def test_case_completion_requires_every_claim_to_be_signed(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _case_review_completion(result, decisions_map=None)", source)
        self.assertIn('return f"◌ 复核进行中 {signed_count}/{claim_count}"', source)
        self.assertIn('f"复核进行中（{signed_count}/{claim_count}）"', source)
        self.assertNotIn('signed = "decision" in st.session_state', source)

    def test_signed_claim_can_continue_to_next_claim_and_scroll_to_selector(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def review_page(result)")
        end = source.index("def evidence_graph_page(result)", start)
        review_page = source[start:end]

        self.assertIn('id="review-claim-selector-anchor"', review_page)
        self.assertIn('st.session_state.pop("scroll_to_review_claim_selector", False)', review_page)
        self.assertIn("target.scrollIntoView({behavior: 'smooth', block: 'start'})", review_page)
        self.assertIn('st.session_state["review_claim_selector"] = label', review_page)
        self.assertIn('st.session_state["scroll_to_review_claim_selector"] = True', review_page)
        self.assertIn('"↑ 继续处理下一笔主张"', review_page)
        self.assertIn('key="continue_next_pending_claim"', review_page)

    def test_all_signed_claims_can_jump_directly_to_audit_page(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def review_page(result)")
        end = source.index("def evidence_graph_page(result)", start)
        review_page = source[start:end]

        self.assertIn("elif signed_notice and len(claims_list) > 1 and not next_pending:", review_page)
        self.assertIn('"前往 04 审查底稿留痕"', review_page)
        self.assertIn('key="go_to_audit_after_all_claims"', review_page)
        self.assertIn('st.session_state["nav_page"] = PAGE_AUDIT', review_page)
        self.assertIn('on_click=_go_to_audit_after_review', review_page)
        self.assertNotIn('_navigate_to_page(PAGE_AUDIT, "review_complete_jump")', review_page)

    def test_parse_completion_jumps_to_review_page_with_a_notice(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('st.session_state["parse_notice"]', source)
        self.assertIn('st.session_state["nav_page"] = PAGE_REVIEW', source)
        self.assertIn('st.session_state.pop("parse_notice", None)', source)

    def test_uploaded_case_parse_has_provider_specific_running_feedback(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index('if st.button("开始解析案卷"')
        end = source.index('        st.caption("从本地 SQLite 数据库中调阅', start)
        upload_flow = source[start:end]

        status_pos = upload_flow.index("parse_status = st.status(")
        run_pos = upload_flow.index("result = run_case_inputs(")
        self.assertLess(status_pos, run_pos)
        self.assertIn('if provider_name == "deepseek":', upload_flow)
        self.assertIn("正在等待 DeepSeek 返回结构化解析结果", upload_flow)
        self.assertIn("请勿重复提交或刷新页面", upload_flow)
        self.assertIn("正在执行本地模拟解析与证据核验", upload_flow)

    def test_uploaded_case_parse_closes_status_on_success_and_failure(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index('if st.button("开始解析案卷"')
        end = source.index('        st.caption("从本地 SQLite 数据库中调阅', start)
        upload_flow = source[start:end]

        self.assertIn('label="案卷解析完成，正在进入资金证据核验"', upload_flow)
        self.assertIn('state="complete"', upload_flow)
        self.assertIn('label="案卷解析未完成"', upload_flow)
        self.assertIn('state="error"', upload_flow)
        self.assertIn('st.session_state["nav_page"] = PAGE_REVIEW', upload_flow)
        self.assertIn("st.session_state.failed_audit_events", upload_flow)

    def test_batch_toolbar_appears_once_with_task_framing(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("_render_candidate_batch_toolbar(candidates"), 2)
        self.assertIn("采纳系统建议", source)
        self.assertIn("恢复初始建议", source)
        self.assertIn("对候选流水逐笔作出处置决定", source)
        self.assertNotIn("一键预填处置建议", source)

    def test_key_actions_queue_toast_feedback(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _queue_toast(", source)
        self.assertIn("def _flush_toasts(", source)
        self.assertIn("_flush_toasts()", source)
        # 核准、签署、跳转、批量处置都必须有悬浮回执，不能让用户感觉点了没反应
        self.assertIn("已核准「{claim_label}」起诉事实", source)
        self.assertIn("笔主张核验签署，还剩", source)
        self.assertIn("已切换到「", source)
        self.assertIn("_queue_toast(notice)", source)



if __name__ == "__main__":
    unittest.main()

