"""Remove Patch.url

Revision ID: 52bcea82b482
Revises: 346c011ca77a
Create Date: 2013-11-11 17:17:49.008228

"""

# revision identifiers, used by Alembic.
revision = '52bcea82b482'
down_revision = '346c011ca77a'

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('patch', u'url')
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('patch', sa.Column(u'url', sa.VARCHAR(length=200), nullable=True))
    ### end Alembic commands ###