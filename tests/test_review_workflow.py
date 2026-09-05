from fastapi.testclient import TestClient

from app.main import create_app
from app.services.classifier import classify_path, confidence_level
from test_zip_analyzer import make_zip

WRITE = {"X-PSC-Request": "1"}


def create_project(client):
    response = client.post("/api/projects", json={"name": "Review", "code": "REV-001"}, headers=WRITE)
    assert response.status_code == 201
    return response.json()["id"]


def import_files(client, pid, files):
    raw = make_zip(files).getvalue()
    staged = client.post(
        f"/api/projects/{pid}/imports",
        files={"file": ("review.zip", raw)},
        headers=WRITE,
    )
    assert staged.status_code == 201
    iid = staged.json()["id"]
    confirmed = client.post(f"/api/projects/{pid}/imports/{iid}/confirm", headers=WRITE)
    assert confirmed.status_code == 200


def test_confidence_bands_are_conservative():
    explicit = classify_path("Сертификаты/паспорт трубы.pdf")
    coded = classify_path("СТРМ-ННП.24126-ННП-001-ЛТ01-ГЧ-003_3.pdf")
    generic_image = classify_path("scan-001.jpg")
    unknown = classify_path("notes.bin")

    assert explicit.category == "Сертификаты и паспорта"
    assert confidence_level(explicit.confidence) == "high"
    assert coded.category == "Проект"
    assert confidence_level(coded.confidence) == "medium"
    assert confidence_level(generic_image.confidence) == "low"
    assert unknown.category == "На ручную проверку"
    assert confidence_level(unknown.confidence) == "low"


def test_bulk_review_only_confirms_high_confidence_and_duplicate_view(tmp_path):
    with TestClient(create_app(tmp_path, password="")) as client:
        pid = create_project(client)
        import_files(client, pid, {
            "Сертификаты/паспорт трубы.pdf": b"identical",
            "Сертификаты/копия паспорта.pdf": b"identical",
            "СТРМ-ННП.24126-ННП-001-ЛТ01-ГЧ-003_3.pdf": b"project",
            "notes.bin": b"unknown",
        })

        before = client.get(f"/api/projects/{pid}").json()["summary"]
        assert before["high_pending"] == 2
        assert before["medium_pending"] == 1
        assert before["low_pending"] == 1
        assert before["duplicate_groups"] == 1
        assert before["duplicate_files"] == 2
        assert before["confirmed_sections"] == 0

        response = client.post(
            f"/api/projects/{pid}/documents/bulk-review",
            json={"min_score": 0.90, "category": ""},
            headers=WRITE,
        )
        assert response.status_code == 200, response.text
        assert response.json()["changed"] == 2

        after = client.get(f"/api/projects/{pid}").json()["summary"]
        assert after["high_pending"] == 0
        assert after["medium_pending"] == 1
        assert after["low_pending"] == 1
        assert after["confirmed_sections"] == 1

        rows = client.get(f"/api/projects/{pid}/documents").json()["items"]
        by_name = {row["name"]: row for row in rows}
        assert by_name["паспорт трубы.pdf"]["reviewed"] == 1
        assert by_name["копия паспорта.pdf"]["reviewed"] == 1
        assert by_name["СТРМ-ННП.24126-ННП-001-ЛТ01-ГЧ-003_3.pdf"]["reviewed"] == 0
        assert by_name["notes.bin"]["reviewed"] == 0

        page = client.get(f"/projects/{pid}/documents", params={"duplicates": "true"})
        assert page.status_code == 200
        assert "Группы точных дублей" in page.text
        assert "Группа 1" in page.text
        assert "2 копии" in page.text
        assert "паспорт трубы.pdf" in page.text
        assert "копия паспорта.pdf" in page.text

        assert client.post(
            f"/api/projects/{pid}/documents/bulk-review",
            json={"min_score": 0.89, "category": ""},
            headers=WRITE,
        ).status_code == 422


def test_bulk_review_can_be_scoped_to_one_category(tmp_path):
    with TestClient(create_app(tmp_path, password="")) as client:
        pid = create_project(client)
        import_files(client, pid, {
            "Сертификаты/паспорт.pdf": b"a",
            "Геодезия/исполнительная съемка.pdf": b"b",
        })
        response = client.post(
            f"/api/projects/{pid}/documents/bulk-review",
            json={"min_score": 0.90, "category": "Сертификаты и паспорта"},
            headers=WRITE,
        )
        assert response.status_code == 200
        assert response.json()["changed"] == 1
        summary = client.get(f"/api/projects/{pid}").json()["summary"]
        assert summary["high_pending"] == 1
