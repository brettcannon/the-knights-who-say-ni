import enum
import http
from http import client
import json
import operator
from urllib import parse

from aiohttp import hdrs

from . import abc


LABEL_PREFIX = 'CLA: '
CLA_OK = LABEL_PREFIX + '☑'
NO_CLA = LABEL_PREFIX + '☐'

NO_CLA_TEMPLATE = """Hello, and thanks for your contribution!

{body}

Once you have done everything that's needed, please reply here and someone will
verify everything is in order.

Also, please read the
[Python's Developer Guide](https://docs.python.org/devguide/) if you have not
already.
"""

NO_CLA_BODY = """Unfortunately our records indicate you have not signed a
[PSF contributor agreement](https://www.python.org/psf/contrib/contrib-form/)
(CLA). For legal reasons we need you to sign this before we can look at your
contribution."""

NO_USERNAME_BODY = """Unfortunately we couldn't find an account corresponding
to your GitHub username at [bugs.python.org](http://bugs.python.org/) (b.p.o).
If you don't already have an account at b.p.o, please
[create one](http://bugs.python.org/user?@template=register) and make sure to
add your GitHub username. If you do already have an account at b.p.o then
please go there and under "Your Details" add your GitHub username.

And in case you haven't already, please make sure to sign the
[PSF contributor agreement](https://www.python.org/psf/contrib/contrib-form/)
(CLA); we can't legally look at your contribution until you have signed the
CLA."""


@enum.unique
class PullRequestEvent(enum.Enum):
    # https://developer.github.com/v3/activity/events/types/#pullrequestevent
    assigned = "assigned"
    unassigned = "unassigned"
    labeled = "labeled"
    unlabeled = "unlabeled"
    opened = "opened"
    closed = "closed"
    reopened = "reopened"
    synchronize = "synchronize"


class Host(abc.ContribHost):

    """Implement a webhook for GitHub pull requests."""

    route = 'POST', '/github'

    _useful_actions =  {PullRequestEvent.opened.value,
                        PullRequestEvent.unlabeled.value,
                        PullRequestEvent.synchronize.value}

    def __init__(self, event, request):
        """Represent a contribution."""
        self.event = event
        self.request = request

    @classmethod
    async def process(cls, request):
        """Process the pull request."""
        # https://developer.github.com/webhooks/creating/#content-type
        if request.content_type != 'application/json':
            msg = ('can only accept application/json, '
                   'not {}').format(request.content_type)
            raise abc.ResponseExit(
                    status=http.HTTPStatus.UNSUPPORTED_MEDIA_TYPE, text=msg)

        payload = await request.json()
        if 'zen' in payload:
            # A ping event; nothing to do.
            # https://developer.github.com/webhooks/#ping-event
            raise abc.ResponseExit(status=http.HTTPStatus.NO_CONTENT)
        elif payload['action'] not in cls._useful_actions:
            raise abc.ResponseExit(status=http.HTTPStatus.NO_CONTENT)
        elif payload['action'] == PullRequestEvent.opened.value:
            return cls(PullRequestEvent.opened, payload)
        elif payload['action'] == PullRequestEvent.unlabeled.value:
            label = payload['label']['name']
            if not label.startswith(LABEL_PREFIX):
                raise abc.ResponseExit(status=http.HTTPStatus.NO_CONTENT)
            return cls(PullRequestEvent.unlabeled, payload)
        elif payload['action'] == PullRequestEvent.synchronize.value:
            return cls(PullRequestEvent.synchronize, payload)
        else:  # pragma: no cover
            # Should never happen.
            msg = "don't know how to handle a {!r} event".format(
                payload['action'])
            raise TypeError(msg)

    @staticmethod
    def check_response(response):
        if response.status >= 300:
            msg = 'unexpected response: {}'.format(response.status)
            raise client.HTTPException(msg)

    async def get(self, url: str):
        """Make a GET request for some JSON data.

        Abstracted out for easy testing w/o requiring internet access.
        """
        async with abc.session().get(url) as response:
            self.check_response(response)
            return (await response.json())

    async def post(self, url: str, payload):
        """Make a POST request with JSON data to a URL."""
        encoding = 'utf-8'
        encoded_json = json.dumps(payload).encode(encoding)
        header = {hdrs.CONTENT_TYPE: 'application/json; charset=' + encoding}
        post_manager = abc.session().post(url, data=encoded_json,
                                          headers=header)
        async with post_manager as response:
            self.check_response(response)

    async def delete(self, url):
        """Make a DELETE request to a URL."""
        async with abc.session().delete(url) as response:
            self.check_response(response)

    async def usernames(self):
        """Return an iterable with all of the contributors' usernames."""
        pull_request = self.request['pull_request']
        # Start with the author of the pull request.
        logins = {pull_request['user']['login']}
        # Fetch the commit data for the pull request.
        commits = await self.get(pull_request['commits_url'])
        # For each commit, get the author and committer.
        for commit in commits:
            logins.add(commit['author']['login'])
            logins.add(commit['committer']['login'])
        return frozenset(logins)

    async def labels_url(self, label=None):
        """Construct the URL to the label."""
        if not hasattr(self, '_labels_url'):
            issue_url = self.request['pull_request']['issue_url']
            issue_data = await self.get(issue_url)
            self._labels_url = issue_data['labels_url']
        quoted_label = ''
        if label is not None:
            quoted_label = '/' + parse.quote(label)
        mapping = {'/name': quoted_label}
        return self._labels_url.format_map(mapping)

    async def current_label(self):
        """Return the current CLA-related label."""
        labels_url = await self.labels_url()
        all_labels = map(operator.itemgetter('name'),
                         await self.get(labels_url))
        cla_labels = (x for x in all_labels if x.startswith(LABEL_PREFIX))
        cla_labels = sorted(cla_labels)
        return cla_labels[0] if len(cla_labels) > 0 else None

    async def set_label(self, status):
        """Set the label on the pull request based on the status of the CLA."""
        labels_url = await self.labels_url()
        if status == abc.Status.signed:
            await self.post(labels_url, [CLA_OK])
            return CLA_OK
        else:
            await self.post(labels_url, [NO_CLA])
            return NO_CLA

    async def remove_label(self):
        """Remove any CLA-related labels from the pull request."""
        cla_label = await self.current_label()
        if cla_label is None:
            return None
        deletion_url = await self.labels_url(cla_label)
        await self.delete(deletion_url)
        return cla_label

    async def comment(self, status):
        """Add an appropriate comment relating to the CLA status."""
        comments_url = self.request['pull_request']['comments_url']
        if status == abc.Status.signed:
            return None
        elif status == abc.Status.not_signed:
            message = NO_CLA_TEMPLATE.format(body=NO_CLA_BODY)
        elif status == abc.Status.username_not_found:
            message = NO_CLA_TEMPLATE.format(body=NO_USERNAME_BODY)
        else:  # pragma: no cover
            # Should never be reached.
            raise TypeError("don't know how to handle {}".format(status))
        await self.post(comments_url, {'body': message})
        return message

    async def update(self, status):
        if self.event == PullRequestEvent.opened:
            await self.set_label(status)
            await self.comment(status)
        elif self.event == PullRequestEvent.unlabeled:
            # The assumption is that a PR will almost always go from no CLA to
            # being cleared, so don't bug the user with what will probably
            # amount to a repeated message about lacking a CLA.
            await self.set_label(status)
        elif self.event == PullRequestEvent.synchronize:
            current_label = await self.current_label()
            if status == abc.Status.signed:
                if current_label != CLA_OK:
                    await self.remove_label()
            elif current_label != NO_CLA:
                    await self.remove_label()
                    # Since there is a chance a new person was added to a PR
                    # which caused the change in status, a comment on how to
                    # resolve the CLA issue is probably called for.
                    await self.comment(status)
        else:  # pragma: no cover
            # Should never be reached.
            msg = 'do not know how to update a PR for {}'.format(self.event)
            raise RunimeError(msg)
