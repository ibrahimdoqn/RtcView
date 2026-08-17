# gevent's monkey-patching has to run before anything else in the process
# touches socket/ssl/threading/subprocess -- `python -m app.main` imports
# this package first, so this is the earliest hook available. Patching late
# (e.g. inside main()) would leave any module already imported by then
# holding unpatched references, defeating cooperative concurrency for it.
# See CHANGELOG's WebSocket-relay entry for why this is here at all: the
# live-view relay needs a WSGI server that supports WebSocket upgrades,
# which waitress does not.
import gevent.monkey

gevent.monkey.patch_all()
