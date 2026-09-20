from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "ui" / "streamlit_app.py"
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

    def test_key_is_auto_verified_when_entered(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("Auto-verify each new key", source)
        self.assertIn('st.session_state.get("deepseek_key_verified")', source)
        self.assertIn("_verify_deepseek_key()", source)
        # 结果紧跟输入框下方，不需用户点按钮就能看到连接状态
        self.assertIn("st.success(message)", source)
        self.assertIn("st.warning(message)", source)

    def test_container_injected_key_stays_as_fallback(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('DEEPSEEK_ENV_KEY = os.environ.get("DEEPSEEK_API_KEY", "")', source)
        self.assertIn('os.environ["DEEPSEEK_API_KEY"] = DEEPSEEK_ENV_KEY', source)

    def test_missing_key_error_is_actionable(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('if "DEEPSEEK_API_KEY" in str(exc):', source)
        self.assertIn("请展开左侧【⚙ 模型与规则配置】粘贴密钥", source)
        self.assertIn("或切换回【本地模拟（推荐演示）】", source)

class ReviewFlowRegressionTest(unittest.TestCase):
    def test_supersedes_is_scoped_to_the_current_claim(self):
        """多主张案件中，会话里保存的可能是另一主张的已签署决定；跨主张直接作为
        supersedes 传入会触发 SUPERSEDES_CLAIM_MISMATCH，签署路径必须按主张过滤。"""
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('getattr(prior_decision, "claim_id", None) == claim.id', source)
        self.assertNotIn("supersedes=st.session_state.get(\"decision\")", source)
        self.assertIn("复核版本链与当前主张不一致", source)

    def test_multi_claim_progress_banner_is_shown_on_review_and_audit_pages(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("def _render_multi_claim_progress(result, claims_list)", source)
        self.assertIn("多主张案件进度提醒", source)
        self.assertIn("HUMAN_CONFIRMED", source)
        self.assertEqual(source.count("_render_multi_claim_progress(result, claims_list)"), 3)

    def test_parse_completion_jumps_to_review_page_with_a_notice(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('st.session_state["parse_notice"]', source)
        self.assertIn('st.session_state["nav_page"] = PAGE_REVIEW', source)
        self.assertIn('st.session_state.pop("parse_notice", None)', source)

    def test_batch_toolbar_appears_once_with_task_framing(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("_render_candidate_batch_toolbar(candidates"), 2)
        self.assertIn("一键套用系统建议", source)
        self.assertIn("恢复初始建议", source)
        self.assertIn("对候选流水逐笔作出处置决定", source)
        self.assertNotIn("一键预填处置建议", source)



if __name__ == "__main__":
    unittest.main()

