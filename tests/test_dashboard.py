from fastapi.testclient import TestClient

from factory.__main__ import main
from factory.dashboard.app import app


def test_ten_manual_ideas_show_on_dashboard(conn):
    assert main(["seed-sample"]) == 0
    html = TestClient(app).get("/").text
    for n in range(1, 11):
        assert f"I-{n:05d}" in html
    assert "探索 / 案登録" in html
    assert "1. 要判断" in html and "5. 否決メモ" in html


def test_filter_and_detail(conn):
    main(["seed-sample"])
    main(["add-memo", "--idea", "I-00002", "--stage", "調査", "--tag", "機会不足", "--reason", "30日で成立4回"])
    client = TestClient(app)
    html = client.get("/", params={"stage": "否決"}).text
    assert "I-00002" in html and "I-00003" not in html.split("案の一覧")[1].split("5. 否決メモ")[0]
    assert "機会不足" in client.get("/ideas/I-00002").text
    assert client.get("/ideas/I-99999").status_code == 404


def test_paper_bots_and_costs_panels(conn, fake_ai, reader):
    from tests.test_implement_paper import ready_idea
    from factory.market import Market
    from factory.stages import implement

    idea = ready_idea(conn)
    implement.run(conn, Market(conn, reader))
    client = TestClient(app)
    html = client.get("/").text
    assert "4. paper評価中" in html and "B-0001" in html and "開始待ち" in html
    assert "6. 費用" in html and "<td>実装</td>" in html and "クレジット残高の見込み" in html
    detail = client.get(f"/ideas/{idea}").text
    assert "BOT B-0001" in detail and "paper評価中" in detail
