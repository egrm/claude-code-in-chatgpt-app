"""Client leases: the owned worker lives only while ChatGPT/MCP clients are connected.

Facades heartbeat every HEARTBEAT_S; a lease lapses LEASE_TTL_S after its last heartbeat.
Work RPCs must carry a live lease; requests themselves never extend it. With no live lease
the worker waits IDLE_GRACE_S for a reconnect, then drains: new operations and answers are
refused, active tasks are interrupted (explicit resume later) and their Claude processes are
closed within CLOSE_TIMEOUT_S. Worst case after abrupt client loss: TTL + grace = 16 s after the
last heartbeat, plus bounded cleanup. A worker nobody connects to exits after STARTUP_GRACE_S.
Stdlib only (the installer imports PROTOCOL).
"""
import math
import os
import secrets
import time

PROTOCOL = 2
HEARTBEAT_S = 2.0
LEASE_TTL_S = 6.0
IDLE_GRACE_S = 10.0
STARTUP_GRACE_S = 30.0
CLOSE_TIMEOUT_S = 8.0
DRAINING = 'Supervisor worker is shutting down; reconnect and resume tasks explicitly'
RELOAD = 'Client lease required: reload/refresh the claude_supervisor MCP server so its facade is current'


def configured():
    """Defaults, or CCS_LIFECYCLE="ttl,grace,startup,close" for fixtures; values may only shorten."""
    defaults = {'ttl': LEASE_TTL_S, 'grace': IDLE_GRACE_S, 'startup_grace': STARTUP_GRACE_S, 'close_timeout': CLOSE_TIMEOUT_S}
    try:
        values = [float(v) for v in os.environ.get('CCS_LIFECYCLE', '').split(',')]
    except ValueError:
        return defaults
    if len(values) != 4 or not all(math.isfinite(v) and 0 < v <= d for v, d in zip(values, defaults.values())):
        return defaults
    return dict(zip(defaults, values))


class Lifecycle:
    def __init__(self, ttl=LEASE_TTL_S, grace=IDLE_GRACE_S, startup_grace=STARTUP_GRACE_S,
                 close_timeout=CLOSE_TIMEOUT_S, clock=time.monotonic):
        self.ttl, self.grace, self.close_timeout, self.clock = ttl, grace, close_timeout, clock
        self.leases = {}
        self.state = 'running'
        self.idle_since, self.idle_limit = clock(), startup_grace

    def _tick(self):
        """Prune lapsed leases and flip to draining at the deadline, whether or not the watchdog ran."""
        now = self.clock()
        expired = [lease for lease, (expiry, _) in self.leases.items() if expiry <= now]
        if expired:
            last = max(self.leases.pop(lease)[0] for lease in expired)
            if not self.leases:
                self.idle_since, self.idle_limit = last, self.grace
        if self.state == 'running' and not self.leases and now - self.idle_since >= self.idle_limit:
            self.state = 'draining'
        return now

    def _running(self):
        now = self._tick()
        if self.state != 'running':
            raise ValueError(DRAINING)
        return now

    def acquire(self, client=''):
        now = self._running()
        lease = secrets.token_urlsafe(16)
        self.leases[lease] = (now + self.ttl, str(client)[:200])
        return {'lease_id': lease, 'ttl_s': self.ttl, 'heartbeat_s': min(HEARTBEAT_S, self.ttl / 3), 'idle_grace_s': self.grace}

    def heartbeat(self, lease):
        now = self._running()
        if lease not in self.leases:
            raise ValueError('Unknown or expired client lease; acquire a new one')
        self.leases[lease] = (now + self.ttl, self.leases[lease][1])
        return {'ok': True}

    def release(self, lease):
        now = self._tick()
        if self.leases.pop(lease, None) is not None and not self.leases:
            self.idle_since, self.idle_limit = now, self.grace
        return {'released': True}

    def validate(self, lease):
        """Work RPC admission; not a heartbeat."""
        self._running()
        if not lease or lease not in self.leases:
            raise ValueError(RELOAD if not lease else 'Unknown or expired client lease; reconnect the MCP server')

    def clients(self):
        self._tick()
        return len(self.leases)

    def idle_expired(self):
        self._tick()
        return self.state == 'draining'
