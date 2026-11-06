"""チェーンをまとめて調べる(chain-survey): 3つの道具を順に動かし、1チェーン1行でまとめる。失敗したチェーンは飛ばす。"""
from factory.onchain import scan_command
from tests.test_onchain_actors import make_chain
from tests.test_onchain_arbs import setup


def test_survey_runs_each_tool_and_skips_a_broken_chain(conn):
    node, _ = setup()
    node.add_liquidation(6_000)                            # Aave の清算 1件(50ドル − ガス代)

    def factory(name):
        if name == "broken":
            raise ConnectionError("つながらない")
        return make_chain(node)

    lines: list[str] = []
    assert scan_command.run_survey(["arbitrum", "broken"], days=1, hours=7, top=3, conn=conn,
                                   out=lines.append, chain_factory=factory) == 0
    text = "\n".join(lines)
    assert "===== 1/2 arbitrum =====" in text and "===== 2/2 broken =====" in text
    assert "broken の裁定は読めなかった(ConnectionError: つながらない)" in text
    summary = text.split("===== まとめ")[1]
    assert "arbitrum:  Aave の清算 1件・1主体・純利益 $49.97、1位 100%" in summary
    assert "清算の仕組み 1か所・1件" in summary and "裁定 4件・3主体・粗利 $45.00、1位 89%" in summary
    assert "broken:  読めなかった" in summary
