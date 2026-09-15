// Command gateway runs the piso MITM gateway: transparent TCP/443 intercept
// (SNI → MITM ruled hosts / splice unruled), the control-plane web UI + API,
// the worker API, and the ingress reverse proxy for name.piso.local routes.
//
// Flags follow the convention: everything is overridable by the CLI/compose.
package main

import (
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"time"

	"piso/gateway/internal/proxy"
	"piso/gateway/internal/server"
	"piso/gateway/internal/store"
)

// workerIdentityFn returns the request→worker-identity callback. It resolves
// the request's origin IP against the worker registry (populated by the host
// CLI at `piso up`) and returns the worker slug.
func workerIdentityFn(st *store.Store) func(*http.Request) string {
	return func(r *http.Request) string {
		slug, ok := st.WorkerByIP(remoteIP(r))
		if ok {
			return slug.Slug
		}
		return ""
	}
}

func remoteIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return ""
	}
	return host
}

func main() {
	var (
		statePath      = flag.String("state", envOr("PISO_STATE_FILE", ".piso/state.json"), "state file (secrets/rules/domains/exceptions/routes)")
		patternsPath   = flag.String("patterns", envOr("PISO_PATTERNS_FILE", ".piso/patterns.json"), "credential pattern library file")
		logPath        = flag.String("log", envOr("PISO_LOG_FILE", ".piso/requests.jsonl"), "request log (JSONL)")
		activitiesPath = flag.String("activities", envOr("PISO_ACTIVITIES_FILE", ".piso/activities.db"), "activity database (SQLite)")
		caCertPath     = flag.String("ca-cert", envOr("PISO_CA_CERT", ".piso/ca.crt"), "CA certificate (generated if missing)")
		caKeyPath      = flag.String("ca-key", envOr("PISO_CA_KEY", ".piso/ca.key"), "CA private key (generated if missing)")
		ctrlAddr       = flag.String("ctrl-listen", envOr("PISO_CTRL_LISTEN", ":8081"), "control plane (UI+API) listen addr")
		workerAddr     = flag.String("worker-listen", envOr("PISO_WORKER_LISTEN", ":8083"), "worker API listen addr (vpc only)")
		ingressAddr    = flag.String("ingress-listen", envOr("PISO_INGRESS_LISTEN", ":8082"), "ingress reverse proxy listen addr")
		maxLog         = flag.Int("max-log", 5000, "max in-memory log records")
		transparent    = flag.String("transparent-listen", envOr("PISO_TRANSPARENT_LISTEN", ":8084"), "SNI intercept listen addr (vpc :443)")
	)
	flag.Parse()

	if err := os.MkdirAll(".piso", 0o700); err != nil {
		log.Fatalf("mkdir: %v", err)
	}

	st, err := store.New(*statePath, *logPath, *patternsPath, *activitiesPath, *maxLog)
	if err != nil {
		log.Fatalf("store: %v", err)
	}

	pat, err := st.LoadPatterns()
	if err != nil {
		log.Fatalf("patterns: %v", err)
	}

	ca, err := proxy.LoadCA(*caCertPath, *caKeyPath)
	if err != nil {
		log.Fatalf("ca: %v", err)
	}

	h := proxy.New(ca, st, pat, workerIdentityFn(st))
	go func() {
		if err := h.ServeTransparentLoop(*transparent); err != nil {
			log.Fatalf("transparent listener: %v", err)
		}
	}()
	srv := server.New(st, pat, h, ca)

	// WebHandler is the single host web entrypoint (dashboard + ingress on the
	// same port, dispatched by Host). IngressHandler is kept on the ingress
	// port for backward compatibility.
	go serve("web", *ctrlAddr, srv.WebHandler())
	go serve("worker-api", *workerAddr, srv.WorkerHandler())
	serve("ingress", *ingressAddr, srv.IngressHandler())
}

func serve(name, addr string, h http.Handler) {
	l, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("%s: listen %s: %v", name, addr, err)
	}
	log.Printf("%s listening on %s", name, addr)
	srv := &http.Server{
		Handler:           h,
		ReadHeaderTimeout: 10 * time.Second,
	}
	if err := srv.Serve(l); err != nil {
		log.Fatalf("%s: %v", name, err)
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
