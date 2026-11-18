"""자주 쓰는 조회용 인덱스 (서버 시작 때 한 번, 이미 있으면 그대로).

인덱스가 하나도 없어 매 요청의 로그인 확인(세션 조회)부터 작업 · 문서 조회까지 모두 컬렉션 전체를 훑고 있었다.
"""

from pymongo import ASCENDING, DESCENDING

from app.db import docs_col, history_col, sessions_col, users_col, whisper_notes_col

SESSION_TTL_SECONDS = 8 * 24 * 3600  # 로그인 쿠키(7일)보다 조금 길게


def ensure_indexes() -> None:
    specs = [
        (sessions_col, [("session_id", ASCENDING)], {}),
        (sessions_col, [("username", ASCENDING)], {}),
        # created_at 이 있는 세션만 만료된다 (예전 세션은 그대로 둠)
        (
            sessions_col,
            [("created_at", ASCENDING)],
            {"expireAfterSeconds": SESSION_TTL_SECONDS},
        ),
        (users_col, [("username", ASCENDING)], {}),
        (users_col, [("email", ASCENDING)], {}),
        (history_col, [("id", ASCENDING)], {}),
        (history_col, [("owner", ASCENDING), ("created_at", DESCENDING)], {}),
        (history_col, [("status", ASCENDING)], {}),
        (docs_col, [("id", ASCENDING)], {}),
        (docs_col, [("owner", ASCENDING), ("parent_id", ASCENDING)], {}),
        (whisper_notes_col, [("uid", ASCENDING)], {}),
        (whisper_notes_col, [("owner", ASCENDING), ("createdAt", DESCENDING)], {}),
    ]
    for col, keys, opts in specs:
        try:
            col.create_index(keys, background=True, **opts)
        except Exception as error:  # 인덱스가 없어도 동작은 한다
            print(f"[Warn] index {col.name} {keys}: {error}")
