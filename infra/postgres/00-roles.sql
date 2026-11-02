-- 출발지·목적지 공통 역할
-- chat_app: 앱 전용 업무 쓰기 역할 (펜싱 대상)
-- replicator: 논리 복제 접속 전용
-- 제어 역할은 데모 단순화를 위해 superuser(postgres)를 쓴다.
CREATE ROLE chat_app LOGIN PASSWORD 'chat_app_pw';
CREATE ROLE replicator LOGIN REPLICATION PASSWORD 'replicator_pw';
