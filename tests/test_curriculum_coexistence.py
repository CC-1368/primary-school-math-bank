"""本项目只发行人教版目录：任何外来目录都按 A 落库，不产生第二份映射。"""
from main import LOCAL_TOKEN
from mathbank.database import QuestionCurriculum, init_db


A_CURRICULUM = {
    "必修一": {"1. 集合与常用逻辑用语": ["1.1"]},
    "必修二": {"6. 平面向量及其应用": ["6.1"]},
}


def _rows(db_session, question_id):
    db_session.expire_all()
    return [
        (row.version_code, row.compulsory, row.chapter, row.knowledge)
        for row in db_session.query(QuestionCurriculum).filter_by(question_id=question_id)
    ]


def test_single_version_mapping_survives_metadata_switch(client, db_session):
    init_db()

    import main
    main.METADATA_CACHE = {
        "question_types": [],
        "difficulties": [],
        "curriculum": {"必修一": {"1. 集合与常用逻辑用语": ["1.1"]}},
    }

    headers = {"X-Local-Token": LOCAL_TOKEN}
    payload = {
        "content": "测试向量题干 $a+b$",
        "question_type": "single_choice",
        "category_compulsory": "必修二",
        "category_chapter": "6. 平面向量及其应用",
        "category_knowledge": "平面向量的数量积",
        "difficulty": "medium",
        "source": "单元测试",
        "answer_markdown": "答案解析",
        "review": "评述",
        "tikz_code": "",
        "tags": "",
        "related_question_id": "",
        "image_paths": "[]",
    }

    response = client.post("/api/questions", data=payload, headers=headers)
    assert response.status_code == 200
    question_id = response.json()["question"]["id"]
    assert _rows(db_session, question_id) == [
        ("A", "必修二", "6. 平面向量及其应用", "平面向量的数量积")
    ]

    # 1. 保存一份「看起来像别的版本」的目录：版本判定仍是 A，
    #    不会凭空多出一份映射，也不会改写已有题目的分类。
    foreign_curriculum = {
        "question_types": main.METADATA_CACHE["question_types"],
        "difficulties": main.METADATA_CACHE["difficulties"],
        "curriculum": {
            "必修一": {"第一章 集合与常用逻辑用语": ["1.1 集合"]},
            "必修二": {"第六章 平面向量初步": ["6.1"]},
            "必修三": {"第八章 向量的数量积与三角恒等变换": ["8.1"]},
        },
    }
    response = client.post("/api/config/metadata", json=foreign_curriculum, headers=headers)
    assert response.status_code == 200
    assert response.json()["curriculum_version"] == "A"
    assert _rows(db_session, question_id) == [
        ("A", "必修二", "6. 平面向量及其应用", "平面向量的数量积")
    ]
    fetched = client.get(f"/api/questions/{question_id}").json()
    assert fetched["category_compulsory"] == "必修二"
    assert fetched["category_chapter"] == "6. 平面向量及其应用"
    assert fetched["category_knowledge"] == "平面向量的数量积"

    # 2. 更新题目分类：只更新那唯一一条 A 映射，不会为别的版本各写一份。
    update_payload = dict(payload)
    update_payload["content"] = "更新向量题干"
    update_payload["category_chapter"] = "第六章 平面向量初步"
    update_payload["category_knowledge"] = "6.1"
    response = client.put(f"/api/questions/{question_id}", data=update_payload, headers=headers)
    assert response.status_code == 200
    assert _rows(db_session, question_id) == [
        ("A", "必修二", "第六章 平面向量初步", "6.1")
    ]

    # 3. 切回人教版目录：映射与题目分类都保持原样，不被清空或替换。
    response = client.post(
        "/api/config/metadata",
        json={
            "question_types": main.METADATA_CACHE["question_types"],
            "difficulties": main.METADATA_CACHE["difficulties"],
            "curriculum": A_CURRICULUM,
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert _rows(db_session, question_id) == [
        ("A", "必修二", "第六章 平面向量初步", "6.1")
    ]
    fetched = client.get(f"/api/questions/{question_id}").json()
    assert fetched["category_compulsory"] == "必修二"
    assert fetched["category_chapter"] == "第六章 平面向量初步"
    assert fetched["category_knowledge"] == "6.1"
