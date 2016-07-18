
import signal
import sys
from functools import partial
from pathlib import Path

from invoke import Collection, Executor, task, run
from invoke.util import cd

import colorama
from termcolor import cprint

import tasks_servers

colorama.init()

try:
    # We import the tasks module of the applications via 'applications.XXX.tasks'; this extends the path so as to allow
    # them to find their companion modules (task_base, task_XXX). (Yes, this is abusing the import machinery a bit,
    # however, it works nicely and is rather simple).
    from applications import block, accounting
    sys.path[0:0] = block.__path__
    sys.path[0:0] = accounting.__path__

    from applications.block.tasks import deploy as deploy_block, try_load
    from applications.accounting.tasks import namespace as accounting
    from applications.drop.tasks import namespace as drop
    from applications.index.tasks import namespace as index
except ImportError:
    cprint('Applications are not up-to-date (inv scripts not found).\n'
           'Run "inv update" to fix.',
           'red', attrs=['bold'])
    deploy_block = try_load = lambda *args: True
    accounting = drop = index = {
        'deploy': lambda *args: None,
        'manage': lambda *args: None
    }
    HAVE_APPS = False
else:
    HAVE_APPS = True


APPS_AND_DEPLOY_TASKS = [
    ('applications/block',
     [
         deploy_block
     ]),
    ('applications/accounting',
     [
         accounting['deploy'],
         partial(accounting['manage'], command='loaddata testdata.json'),
     ]),
    ('applications/drop',
     [
         drop['deploy'],
     ]),
    ('applications/index',
     [
         index['deploy'],
     ]),
]


def print_bold(*args, **kwargs):
    cprint(' '.join(args), attrs=['bold'], **kwargs)


@task(pre=[tasks_servers.start_all])
def deploy(ctx):
    for app, deploy_tasks in APPS_AND_DEPLOY_TASKS:
        with cd(app):
            print_bold('Deploying', app)
            for task in deploy_tasks:
                task(ctx)


@task(
    pre=[deploy],
    help={
        'quiet': 'Smother uWSGI log output',
    },
)
def start(ctx, background=False, quiet=False):
    """
    Run server with uWSGI.

    Note: an explicit "stop" is only needed when run in the background (-b, --background)
          otherwise everything terminates on ^C (SIGINT).
    """
    pidfile = Path(ctx.qabel.testing.app_data) / 'uwsgi.pid'
    pidfile.parent.mkdir(exist_ok=True, parents=True)
    if tasks_servers.pidfile_alive(pidfile):
        print_bold('uWSGI is already running -- killable with "inv stop"')
        return False
    print_bold('Starting uWSGI')
    command_line = [
        'uwsgi',
        '--pidfile', pidfile,
        '--emperor', '"applications/*/deployed/current/uwsgi.ini"',
    ]
    if quiet:
        command_line.append('--logto /dev/null')
    if background:
        command_line.append('&')
    command_line = ' '.join(map(str, command_line))
    print_bold('uWSGI command line:')
    print_bold(command_line)
    run(command_line)
    return True


@task(post=[tasks_servers.stop_all])
def stop(ctx):
    pidfile = Path(ctx.qabel.testing.app_data) / 'uwsgi.pid'
    tasks_servers.kill_pidfile(pidfile, signal.SIGINT)


@task(pre=[tasks_servers.status])
def status(ctx):
    uwsgi_pidfile = Path(ctx.qabel.testing.app_data) / 'uwsgi.pid'
    if tasks_servers.pidfile_alive(uwsgi_pidfile):
        print('uWSGI is started, emperor PID', uwsgi_pidfile.read_text().strip())
    else:
        print('uWSGI is stopped')


@task(
    help={
        'pytest_args': 'Additional arguments passed to py.test',
        'which': 'Testing environment (see config). Default: adhoc.',
        'quiet': 'Smother uWSGI log output',
    }
)
def test(ctx, pytest_args='', which='adhoc', quiet=False):
    """
    Run the test suite against ad-hoc created infrastructure.
    """
    testenv = getattr(ctx.qabel.testing, which)
    start_servers = testenv.get('start_servers', False)
    pallin = Executor(namespace, ctx.config)
    if start_servers:
        # For correct resolution of pre/post tasks this is needed, a bit ugly but oh well.
        result = pallin.execute(
            ('start', {'background': True, 'quiet': quiet})
        )
        start_servers = result[start]  # only stop them if we actually had to start them
    command_line = ['py.test']
    for app, _ in APPS_AND_DEPLOY_TASKS:
        *_, app = app.split('/')
        app_url = '--{app}-url {url}'.format(app=app, url=testenv[app])
        command_line.append(app_url)
    command_line.append(pytest_args)
    command_line = ' '.join(command_line)
    print_bold(command_line)
    try:
        ctx.run(command_line, pty=True)
    finally:
        if start_servers:
            pallin.execute(('stop', {}))


@task
def update(ctx):
    """
    Update applications/* from git origin.
    """
    for app, *_ in APPS_AND_DEPLOY_TASKS:
        papp = Path(app)
        if not papp.exists():
            print_bold('Cloning', app)
            run('git clone https://github.com/Qabel/qabel-{name} {path}'.format(name=papp.name, path=papp))
        with cd(app):
            print_bold('Updating', app)
            run('git pull')


namespace = Collection(deploy, start, stop, status, test, update, tasks_servers.servers)
if not HAVE_APPS:
    namespace = Collection(update)

# Load configuration explicitly
for app, *_ in APPS_AND_DEPLOY_TASKS:
    assert try_load(Path(app) / 'defaults.yaml', namespace)
assert try_load(Path(__file__).with_name('defaults.yaml'), namespace)