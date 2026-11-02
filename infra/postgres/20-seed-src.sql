-- 출발지에만 넣는 기존 업무 데이터. 초기 복사로 목적지에 옮겨진다.
-- 시퀀스는 논리 복제로 옮겨지지 않으므로 목적지 messages_id_seq는 1에 머문다(컷오버 때 보정).
INSERT INTO messages (idem_key, body, attachment_key)
SELECT 'seed-' || g, '기존 상담 메시지 #' || g, CASE WHEN g % 10 = 0 THEN 'sample.txt' END
FROM generate_series(1, 100) AS g;
