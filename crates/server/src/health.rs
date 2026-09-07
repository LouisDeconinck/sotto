//! Liveness and readiness probes.
//!
//! `GET /health` reports that this process is running and nothing more. It touches no
//! dependency, does no work, and is deliberately left exactly as it was: something has been
//! pointed at it since the first deployment, and a liveness probe that starts failing for a
//! reason outside the process is no longer a liveness probe.
//!
//! `GET /health/ready` answers the question a monitor actually needs answering: can this
//! instance serve a request? The two come apart precisely when it matters. Postgres holds every
//! secret, every session and every project, so a database outage fails every real request while
//! leaving `/health` returning `ok`, and an uptime monitor pointed at it stays green for the
//! whole outage. Readiness makes an explicit round trip and reports `503` when it cannot.
//!
//! The body is one word by design. A probe can only act on up or down, so anything richer is
//! reconnaissance for an unauthenticated caller (which dependency failed, and therefore what this
//! deployment runs) and a shape we would then have to keep stable for whoever parsed it.
//!
//! Both routes are unauthenticated. The bundled Caddyfile puts `/health/ready` in the same per-IP
//! rate-limit zone as the other public routes, because an endpoint that queries Postgres for
//! anyone who asks is otherwise an amplification vector: one cheap request in, one pooled
//! connection out. That zone alone does not close it, since the limit is per address and the pool
//! holds eight connections in total, so the verdict is also cached for a moment and concurrent
//! misses share a single in-flight query. A monitor checking every few minutes therefore always
//! measures afresh, while a flood costs at most one query per [`TTL`] no matter how many callers
//! it comes from or how many addresses they hold.

use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use axum::Router;
use sqlx::PgPool;

use crate::state::AppState;

/// How long a verdict may be reused.
///
/// Short enough that any realistic probe interval measures the database afresh every time, and
/// long enough that the cost of a flood is bounded by the clock rather than by the caller.
const TTL: Duration = Duration::from_secs(1);

/// Budget for the whole check, including waiting for a free pooled connection.
///
/// A database that hangs rather than refuses is the case this exists for: sqlx would otherwise
/// wait its default thirty seconds for the pool, holding the request open long after the monitor
/// on the other end has timed out and called the service down anyway. Failing at five seconds
/// reports the same verdict while releasing the connection.
const TIMEOUT: Duration = Duration::from_secs(5);

pub fn router() -> Router<AppState> {
    // One probe per router, captured here rather than parked in `AppState`, so each test builds an
    // app with its own cache and cannot inherit a verdict from an unrelated test.
    let probe = Probe::new();
    Router::new().route("/health", get(live)).route(
        "/health/ready",
        get(move |State(state): State<AppState>| {
            let probe = probe.clone();
            async move { respond(probe.verdict(|| database_answers(&state.pool)).await) }
        }),
    )
}

async fn live() -> &'static str {
    "ok"
}

fn respond(ready: bool) -> (StatusCode, &'static str) {
    if ready {
        (StatusCode::OK, "ok")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "unavailable")
    }
}

/// The cache in front of the check, kept separate from what is being checked so that the sharing
/// rules can be tested without a database and the query can be read without the sharing rules.
#[derive(Clone)]
struct Probe {
    last: Arc<Mutex<Option<Verdict>>>,
    /// Held across the check so concurrent misses queue behind one query instead of taking a
    /// pooled connection each. Waiters re-read the cache on the way in and almost always find the
    /// answer the holder just stored.
    refresh: Arc<tokio::sync::Mutex<()>>,
}

#[derive(Clone, Copy)]
struct Verdict {
    at: Instant,
    ready: bool,
}

impl Probe {
    fn new() -> Self {
        Self {
            last: Arc::new(Mutex::new(None)),
            refresh: Arc::new(tokio::sync::Mutex::new(())),
        }
    }

    async fn verdict<F, Fut>(&self, check: F) -> bool
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = bool>,
    {
        if let Some(ready) = self.fresh() {
            return ready;
        }
        let _refresh = self.refresh.lock().await;
        if let Some(ready) = self.fresh() {
            return ready;
        }
        let ready = check().await;
        self.store(ready);
        ready
    }

    fn fresh(&self) -> Option<bool> {
        let guard = self.last.lock().unwrap_or_else(|e| e.into_inner());
        let last = guard.as_ref()?;
        (last.at.elapsed() < TTL).then_some(last.ready)
    }

    fn store(&self, ready: bool) {
        let mut guard = self.last.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(Verdict {
            at: Instant::now(),
            ready,
        });
    }
}

/// One round trip that a reachable Postgres answers and an unreachable one does not.
///
/// `SELECT 1` fetched as a value rather than executed as a statement, so the server has to send a
/// row back: the point is to prove the connection carries an answer, not merely that a statement
/// was accepted.
async fn database_answers(pool: &PgPool) -> bool {
    let query = sqlx::query_scalar::<_, i32>("SELECT 1").fetch_one(pool);
    match tokio::time::timeout(TIMEOUT, query).await {
        Ok(Ok(_)) => true,
        Ok(Err(e)) => {
            eprintln!("health: readiness query failed: {e}");
            false
        }
        Err(_) => {
            eprintln!("health: readiness query exceeded {TIMEOUT:?}");
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    /// A check that records how often it actually ran, so the tests below can assert what the
    /// cache spared the database rather than only what it returned.
    fn counted(ready: bool) -> (impl Fn() -> std::future::Ready<bool>, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = calls.clone();
        (
            move || {
                seen.fetch_add(1, Ordering::SeqCst);
                std::future::ready(ready)
            },
            calls,
        )
    }

    #[tokio::test]
    async fn first_call_runs_the_check() {
        let probe = Probe::new();
        let (check, calls) = counted(true);
        assert!(probe.verdict(&check).await);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn a_second_call_reuses_the_verdict() {
        let probe = Probe::new();
        let (check, calls) = counted(true);
        probe.verdict(&check).await;
        assert!(probe.verdict(&check).await);
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "a burst must not cost one query each"
        );
    }

    #[tokio::test]
    async fn a_stale_verdict_is_not_reused() {
        let probe = Probe::new();
        let (check, calls) = counted(false);
        probe.verdict(&check).await;
        // Age the stored verdict rather than sleeping through the TTL, so the test states what it
        // means and stays instant.
        let stale = Instant::now()
            .checked_sub(TTL)
            .expect("the monotonic clock has been running for at least the TTL");
        probe.last.lock().unwrap().as_mut().unwrap().at = stale;
        assert!(!probe.verdict(&check).await);
        assert_eq!(
            calls.load(Ordering::SeqCst),
            2,
            "an outage that ends must be noticed"
        );
    }

    #[tokio::test]
    async fn failure_is_cached_like_success() {
        // Otherwise an outage is the one condition under which the endpoint stops being cheap,
        // which is exactly when the instance can least afford the extra load.
        let probe = Probe::new();
        let (check, calls) = counted(false);
        assert!(!probe.verdict(&check).await);
        assert!(!probe.verdict(&check).await);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn the_body_says_only_up_or_down() {
        assert_eq!(respond(true), (StatusCode::OK, "ok"));
        assert_eq!(
            respond(false),
            (StatusCode::SERVICE_UNAVAILABLE, "unavailable")
        );
    }
}
