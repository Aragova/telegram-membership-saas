"""Self-service ISO 4217 currency catalog and per-project opt-in.

Revision ID: 0024_owner_currency_selection
Revises: 0023_owner_network_profiles
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0024_owner_currency_selection"
down_revision: str | None = "0023_owner_network_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"

# ISO 4217 alphabetic currency codes, updated against SIX List One
# (2026-09-17). No withdrawn ANG/BGN/CUC/HRK/SLL/ZWL, test codes
# or special metal/accounting units with undefined minor units. Captured
# as migration data, not a new runtime package or an external API call.
# Existing seeded fiat/crypto assets retain ALL their original metadata.
FIAT_CODES = """
AED AFN ALL AMD AOA ARS AUD AWG AZN
BAM BBD BDT BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD
CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK
DJF DKK DOP DZD
EGP ERN ETB EUR
FJD FKP
GBP GEL GHS GIP GMD GNF GTQ GYD
HKD HNL HTG HUF
IDR ILS INR IQD IRR ISK
JMD JOD JPY
KES KGS KHR KMF KPW KRW KWD KYD KZT
LAK LBP LKR LRD LSL LYD
MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN
NAD NGN NIO NOK NPR NZD
OMR
PAB PEN PGK PHP PKR PLN PYG
QAR
RON RSD RUB RWF
SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL
THB TJS TMT TND TOP TRY TTD TWD TZS
UAH UGX USD USN UYI UYU UYW UZS
VES VND VUV
WST
XAF XCD XCG XOF XPF
YER
ZAR ZMW ZWG
""".split()
ZERO_DECIMALS = set(
    "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI "
    "VND VUV XAF XOF XPF".split()
)
THREE_DECIMALS = set("BHD IQD JOD KWD LYD OMR TND".split())
FOUR_DECIMALS = set("CLF UYW".split())


def upgrade() -> None:
    catalog = sa.table(
        "currency_assets",
        sa.column("code", sa.String(16)),
        sa.column("asset_type", sa.String(16)),
        sa.column("name", sa.String(80)),
        sa.column("symbol", sa.String(16)),
        sa.column("precision", sa.Integer()),
        sa.column("status", sa.String(16)),
    )
    # Insert missing standard fiat codes only. Never update or enable
    # a pre-existing asset: prices/history may depend on its precision.
    for code in FIAT_CODES:
        digits = (0 if code in ZERO_DECIMALS else
                  3 if code in THREE_DECIMALS else
                  4 if code in FOUR_DECIMALS else 2)
        op.execute(
            pg_insert(catalog).values(
                code=code, asset_type="fiat",
                name=code, symbol=code, precision=digits, status="active",
            ).on_conflict_do_nothing(index_elements=["code"])
        )

    op.create_table(
        "project_enabled_currencies",
        sa.Column(
            "project_id", sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "currency_code", sa.String(16),
            sa.ForeignKey("currency_assets.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("project_id", "currency_code"),
    )
    op.execute("ALTER TABLE project_enabled_currencies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_enabled_currencies FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY project_enabled_currencies_tenant_policy
        ON project_enabled_currencies
        USING ({PROJECT} IS NOT NULL AND project_id = {PROJECT})
        WITH CHECK ({PROJECT} IS NOT NULL AND project_id = {PROJECT})
    """)
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime') THEN
            GRANT SELECT, INSERT, DELETE ON project_enabled_currencies
            TO membership_runtime;
          END IF;
        END $$;
    """)


def downgrade() -> None:
    op.drop_table("project_enabled_currencies")
    # Intentionally retain seeded catalog rows: price histories and currency
    # FKs elsewhere could still reference them.
