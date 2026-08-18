"""expand flood annual chance text"""
import sqlalchemy as sa
from alembic import op

revision = "7d2f3c1a9e4b"
down_revision = "b82b7a1a044f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("flood_zones", "annual_chance", type_=sa.String(length=100))


def downgrade() -> None:
    op.alter_column("flood_zones", "annual_chance", type_=sa.String(length=50))
