-- 출발지·목적지 공통 스키마
-- 논리 복제는 DDL을 옮기지 않으므로 목적지에도 같은 스키마를 먼저 만든다.
CREATE TABLE messages (
    id             bigserial PRIMARY KEY,
    idem_key       text        NOT NULL UNIQUE,
    body           text        NOT NULL,
    attachment_key text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- 컷오버·되돌리기 때 제어 역할이 쓰는 표식 행. 상대편 도착을 확인하는 데 쓴다.
CREATE TABLE bravo_barrier (
    id         bigserial PRIMARY KEY,
    epoch      integer     NOT NULL,
    direction  text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON messages TO chat_app;
GRANT USAGE ON SEQUENCE messages_id_seq TO chat_app;
GRANT SELECT ON messages, bravo_barrier TO replicator;

-- 양쪽 모두 publication을 둔다. 출발지 것은 정방향, 목적지 것은 되돌리기(역방향)에 쓴다.
CREATE PUBLICATION bravo_pub FOR ALL TABLES;
