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
        self.assertIn('"五步 Guided Demo"', source)
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



if __name__ == "__main__":
    unittest.main()

