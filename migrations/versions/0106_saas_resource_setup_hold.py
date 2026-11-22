"""Unblock new channel/group setup when the network has no actual overdue bill.

Revision ID: 0106_saas_setup_hold
Revises: 0105_owner_network_home

The per-bot service hold can be 'held' immediately after an account-wide
trial ends, before the first bot invoice is issued by the billing worker.
That hold must still protect real purchases, but must not prevent the owner
from configuring a channel while the central account report truthfully says
no unpaid bills. Keep genuine owner-wide overdue holds in force.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0106_saas_setup_hold"
down_revision: str | None = "0105_owner_network_home"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Replace ONLY the existing trigger function. All triggers, RLS, real
    # invoice rows, payment allocations and checkout functions stay intact.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_guard_new_commerce()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  scoped uuid;
  source_bot uuid;
  bot_count bigint;
  owner_id uuid;
  merchant_id uuid;
  hold_state text;
  checked_at timestamptz:=clock_timestamp();
BEGIN
  IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
  -- NEW is a trigger RECORD: branch before field access. PostgreSQL
  -- can resolve a missing field even if the other AND term is false.
  IF TG_TABLE_NAME='resources' THEN
    IF NEW.resource_type NOT IN (
      'telegram_channel','telegram_group'
    ) THEN RETURN NEW; END IF;
  END IF;
  scoped:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
  IF scoped IS DISTINCT FROM NEW.project_id THEN
    RETURN NEW; -- existing tenant RLS fails closed for foreign projects
  END IF;
  -- Due debt of this owner blocks new bots, even under another project.
  IF TG_TABLE_NAME='bot_instances' THEN
    SELECT p.owner_user_id INTO owner_id FROM public.projects p
    WHERE p.id=NEW.project_id;
    IF owner_id IS NOT NULL AND
       public.platform_saas_owner_overdue_state(
         owner_id,checked_at
       )='held'
    THEN
      RAISE EXCEPTION 'New bot connection is on ViMi Pay owner hold'
        USING ERRCODE='P6711';
    END IF;
    RETURN NEW;
  END IF;
  SELECT p.owner_user_id,p.merchant_id INTO owner_id,merchant_id
  FROM public.projects p WHERE p.id=NEW.project_id;
  IF owner_id IS NULL THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    SELECT b.id INTO source_bot FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id
      AND b.telegram_bot_id=NEW.telegram_bot_id
      AND b.status='registered';
  ELSE
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN RETURN NEW; END IF;
  END IF;
  IF source_bot IS NULL AND TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    -- Even a malformed pre-insert order cannot bypass the hold by
    -- omitting the bot id. A legitimately issued order remains bound
    -- to the original Telegram bot by its own constraints.
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN source_bot:=NULL; END IF;
  END IF;
  IF source_bot IS NULL THEN RETURN NEW; END IF;
  hold_state:=public.platform_saas_bot_hold_state(source_bot,checked_at);
  IF hold_state<>'held' THEN RETURN NEW; END IF;
  -- Missing or lapsed own service coverage holds new checkouts, not
  -- non-commercial setup. Actual overdue account debt still blocks
  -- a new channel, including debt from any other bot in the network.
  -- Preserve the existing project/bot and creator exemptions above.
  IF TG_TABLE_NAME='resources' AND
     public.platform_saas_owner_overdue_state(owner_id,checked_at)<>'held'
  THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'payment_requests','file_sale_manual_orders','file_sale_purchases'
  ) AND public.platform_saas_exemption_state(
      NEW.project_id,source_bot,merchant_id,owner_id,checked_at
  )='exempt' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'New ViMi Pay checkout/connection is on payment hold'
    USING ERRCODE='P6711';
END; $f$
    """)


def downgrade() -> None:
    # Reinstates the original 0086 guard; never mutates the financial ledger.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_guard_new_commerce()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  scoped uuid;
  source_bot uuid;
  bot_count bigint;
  owner_id uuid;
  merchant_id uuid;
  hold_state text;
  checked_at timestamptz:=clock_timestamp();
BEGIN
  IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
  -- NEW is a trigger RECORD: branch before field access. PostgreSQL
  -- can resolve a missing field even if the other AND term is false.
  IF TG_TABLE_NAME='resources' THEN
    IF NEW.resource_type NOT IN (
      'telegram_channel','telegram_group'
    ) THEN RETURN NEW; END IF;
  END IF;
  scoped:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
  IF scoped IS DISTINCT FROM NEW.project_id THEN
    RETURN NEW; -- existing tenant RLS fails closed for foreign projects
  END IF;
  -- Due debt of this owner blocks new bots, even under another project.
  IF TG_TABLE_NAME='bot_instances' THEN
    SELECT p.owner_user_id INTO owner_id FROM public.projects p
    WHERE p.id=NEW.project_id;
    IF owner_id IS NOT NULL AND
       public.platform_saas_owner_overdue_state(
         owner_id,checked_at
       )='held'
    THEN
      RAISE EXCEPTION 'New bot connection is on ViMi Pay owner hold'
        USING ERRCODE='P6711';
    END IF;
    RETURN NEW;
  END IF;
  SELECT p.owner_user_id,p.merchant_id INTO owner_id,merchant_id
  FROM public.projects p WHERE p.id=NEW.project_id;
  IF owner_id IS NULL THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    SELECT b.id INTO source_bot FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id
      AND b.telegram_bot_id=NEW.telegram_bot_id
      AND b.status='registered';
  ELSE
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN RETURN NEW; END IF;
  END IF;
  IF source_bot IS NULL AND TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    -- Even a malformed pre-insert order cannot bypass the hold by
    -- omitting the bot id. A legitimately issued order remains bound
    -- to the original Telegram bot by its own constraints.
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN source_bot:=NULL; END IF;
  END IF;
  IF source_bot IS NULL THEN RETURN NEW; END IF;
  hold_state:=public.platform_saas_bot_hold_state(source_bot,checked_at);
  IF hold_state<>'held' THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'payment_requests','file_sale_manual_orders','file_sale_purchases'
  ) AND public.platform_saas_exemption_state(
      NEW.project_id,source_bot,merchant_id,owner_id,checked_at
  )='exempt' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'New ViMi Pay checkout/connection is on payment hold'
    USING ERRCODE='P6711';
END; $f$
    """)
