--
-- PostgreSQL database dump
--

\restrict 0qZMdsl90W2YJmIp4wyniNW89hXuzXhnwHa1uTjIixrxXzkh7l3AYrijesmE3Ql

-- Dumped from database version 15.14 (Debian 15.14-1.pgdg13+1)
-- Dumped by pg_dump version 15.14 (Debian 15.14-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alerts (
    id bigint NOT NULL,
    level text,
    message text,
    data jsonb,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT alerts_level_check CHECK ((level = ANY (ARRAY['info'::text, 'warning'::text, 'critical'::text, 'success'::text])))
);


--
-- Name: alerts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.alerts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: alerts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.alerts_id_seq OWNED BY public.alerts.id;


--
-- Name: app_schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_schema_migrations (
    id bigint NOT NULL,
    filename text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: app_schema_migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.app_schema_migrations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: app_schema_migrations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.app_schema_migrations_id_seq OWNED BY public.app_schema_migrations.id;


--
-- Name: opportunities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.opportunities (
    id bigint NOT NULL,
    detector text,
    chain text NOT NULL,
    source_tx_hash text,
    features jsonb,
    score numeric,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: opportunities_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.opportunities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: opportunities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.opportunities_id_seq OWNED BY public.opportunities.id;


--
-- Name: ops_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ops_state (
    k text NOT NULL,
    v text NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: pnl_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pnl_daily (
    day date NOT NULL,
    capital_deployed_usd numeric,
    gross_profit_usd numeric,
    gas_usd numeric,
    net_profit_usd numeric,
    win_rate numeric
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    id integer NOT NULL,
    version text NOT NULL,
    applied_at timestamp with time zone DEFAULT now()
);


--
-- Name: schema_migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.schema_migrations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: schema_migrations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.schema_migrations_id_seq OWNED BY public.schema_migrations.id;


--
-- Name: trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trades (
    id bigint NOT NULL,
    mode text NOT NULL,
    chain text NOT NULL,
    tx_hash text,
    status text NOT NULL,
    reason text,
    params jsonb DEFAULT jsonb_build_object() NOT NULL,
    expected_profit_usd numeric,
    realized_profit_usd numeric,
    gas_used bigint,
    gas_price_gwei numeric,
    slippage numeric,
    created_at timestamp with time zone DEFAULT now(),
    executed_at timestamp with time zone,
    ts timestamp with time zone DEFAULT now(),
    token_in text,
    token_out text,
    pair text,
    size_usd numeric,
    gas_usd numeric,
    bundle_tag text,
    builder text,
    block_number bigint,
    inclusion_latency_ms bigint,
    context jsonb DEFAULT jsonb_build_object(),
    realized_pnl_usd numeric,
    CONSTRAINT trades_mode_check CHECK ((mode = ANY (ARRAY['stealth'::text, 'hunter'::text, 'hybrid'::text]))),
    CONSTRAINT trades_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'success'::text, 'failed'::text, 'reverted'::text])))
);


--
-- Name: trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trades_id_seq OWNED BY public.trades.id;


--
-- Name: alerts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts ALTER COLUMN id SET DEFAULT nextval('public.alerts_id_seq'::regclass);


--
-- Name: app_schema_migrations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_schema_migrations ALTER COLUMN id SET DEFAULT nextval('public.app_schema_migrations_id_seq'::regclass);


--
-- Name: opportunities id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunities ALTER COLUMN id SET DEFAULT nextval('public.opportunities_id_seq'::regclass);


--
-- Name: schema_migrations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations ALTER COLUMN id SET DEFAULT nextval('public.schema_migrations_id_seq'::regclass);


--
-- Name: trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades ALTER COLUMN id SET DEFAULT nextval('public.trades_id_seq'::regclass);


--
-- Name: alerts alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_pkey PRIMARY KEY (id);


--
-- Name: app_schema_migrations app_schema_migrations_filename_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_schema_migrations
    ADD CONSTRAINT app_schema_migrations_filename_key UNIQUE (filename);


--
-- Name: app_schema_migrations app_schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_schema_migrations
    ADD CONSTRAINT app_schema_migrations_pkey PRIMARY KEY (id);


--
-- Name: opportunities opportunities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_pkey PRIMARY KEY (id);


--
-- Name: ops_state ops_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ops_state
    ADD CONSTRAINT ops_state_pkey PRIMARY KEY (k);


--
-- Name: pnl_daily pnl_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pnl_daily
    ADD CONSTRAINT pnl_daily_pkey PRIMARY KEY (day);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_version_key UNIQUE (version);


--
-- Name: trades trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_pkey PRIMARY KEY (id);


--
-- Name: idx_opps_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_opps_created ON public.opportunities USING btree (created_at);


--
-- Name: idx_opps_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_opps_score ON public.opportunities USING btree (score);


--
-- Name: idx_trades_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_created ON public.trades USING btree (created_at);


--
-- Name: idx_trades_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_mode ON public.trades USING btree (mode);


--
-- Name: idx_trades_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_status ON public.trades USING btree (status);


--
-- Name: trades_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX trades_ts_idx ON public.trades USING btree (ts DESC);


--
-- PostgreSQL database dump complete
--

\unrestrict 0qZMdsl90W2YJmIp4wyniNW89hXuzXhnwHa1uTjIixrxXzkh7l3AYrijesmE3Ql

