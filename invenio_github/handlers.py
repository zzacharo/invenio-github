# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2023 CERN.
#
# Invenio is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Invenio is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio. If not, see <http://www.gnu.org/licenses/>.
#
# In applying this licence, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization
# or submit itself to any jurisdiction.

"""Implement OAuth client handler."""

from flask import current_app, redirect, url_for
from flask_login import current_user
from invenio_db import db
from invenio_oauth2server.models import Token as ProviderToken
from invenio_oauthclient.models import RemoteToken
from invenio_oauthclient.utils import oauth_link_external_id, oauth_unlink_external_id
from sqlalchemy.orm.exc import NoResultFound

from .api import GitHubAPI
from .models import Repository
from .tasks import disconnect_github


def account_post_init(remote, token=None):
    """Perform post initialization."""
    try:
        gh = GitHubAPI(user_id=token.remote_account.user_id)
        gh.init_account()
        gh.sync()
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(str(e), exc_info=True)


def disconnect(remote):
    """Disconnect callback handler for GitHub."""
    # User must be authenticated
    if not current_user.is_authenticated:
        return current_app.login_manager.unauthorized()

    external_method = "github"
    external_ids = [
        i.id for i in current_user.external_identifiers if i.method == external_method
    ]
    if external_ids:
        oauth_unlink_external_id(dict(id=external_ids[0], method=external_method))

    user_id = int(current_user.get_id())
    token = RemoteToken.get(user_id, remote.consumer_key)
    if token:
        extra_data = token.remote_account.extra_data

        # Delete the token that we issued for GitHub to deliver webhooks
        webhook_token_id = extra_data.get("tokens", {}).get("webhook")
        ProviderToken.query.filter_by(id=webhook_token_id).delete()

        # Disable GitHub webhooks from our side
        db_repos = Repository.query.filter_by(user_id=user_id).all()
        # Keep repositories with hooks to pass to the celery task later on
        repos_with_hooks = [(r.github_id, r.hook) for r in db_repos if r.hook]
        for repo in db_repos:
            Repository.disable(repo)
        # Commit any changes before running the ascynhronous task
        db.session.commit()

        # Send Celery task for webhooks removal and token revocation
        disconnect_github.delay(token.access_token, repos_with_hooks)
        # Delete the RemoteAccount (along with the associated RemoteToken)
        token.remote_account.delete()
        db.session.commit()

    return redirect(url_for("invenio_oauthclient_settings.index"))
