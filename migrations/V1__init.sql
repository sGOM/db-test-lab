-- 회원
CREATE TABLE member (
    id         BIGSERIAL PRIMARY KEY,
    email      VARCHAR(320) NOT NULL,
    name       VARCHAR(50)  NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 유니크 제약: "선조회 후삽입"만으로는 중복을 막지 못한다는 걸 보여주는 근거
ALTER TABLE member ADD CONSTRAINT uk_member_email UNIQUE (email);

-- 주문 ("order" 는 SQL 예약어라 orders 로 둔다)
CREATE TABLE orders (
    id           BIGSERIAL PRIMARY KEY,
    member_id    BIGINT         NOT NULL,
    product_name VARCHAR(100)   NOT NULL,
    quantity     INTEGER        NOT NULL,
    amount       NUMERIC(15, 2) NOT NULL,
    status       VARCHAR(20)    NOT NULL,
    version      BIGINT         NOT NULL DEFAULT 0,
    ordered_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT fk_orders_member FOREIGN KEY (member_id) REFERENCES member (id),
    CONSTRAINT ck_orders_quantity CHECK (quantity > 0)
);

CREATE INDEX idx_orders_member_id ON orders (member_id);
CREATE INDEX idx_orders_status_ordered_at ON orders (status, ordered_at);
