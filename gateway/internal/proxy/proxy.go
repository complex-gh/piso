// Package proxy implements the gateway's MITM egress: a transparent TCP/443
// listener that sniffs TLS SNI, then either terminates TLS (ruled hosts:
// scan, substitute, block) or splices a raw byte tunnel (unruled hosts:
// real end-to-end TLS, no inspection). Blocked requests are captured for replay.
package proxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"regexp"
	"strconv"
	"strings"
	"time"

	"piso/gateway/internal/model"
	"piso/gateway/internal/patterns"
	"piso/gateway/internal/policy"
	"piso/gateway/internal/scanner"
	"piso/gateway/internal/store"
)

// headerTimeout is how long the forwarding client waits for origin response
// headers. LLM routers often hold headers until the model starts; 30s was
// producing empty 502s (http2: timeout awaiting response headers).
const headerTimeout = 90 * time.Second

// streamTimeout bounds a forwarded request including a streamed body.
// Completions can run for minutes after headers; the old 60s deadline
// canceled the origin body mid-stream.
const streamTimeout = 10 * time.Minute

// streamCopyBuf is the io.Copy buffer for tunnel bodies. The default 32KiB
// would hold SSE tokens in the gateway until the buffer filled.
const streamCopyBuf = 1024

// Handler is the egress MITM: SNI intercept, substitution, and splice.
type Handler struct {
	CA       *CA
	Patterns *patterns.Compiled
	Store    *store.Store
	Worker   func(*http.Request) string // request -> worker identity

	client *http.Client
}

// New builds the handler with a forwarding client that never follows
// redirects automatically: redirects are returned to the client so the next
// hop is re-scanned and re-policed by the gateway.
func New(ca *CA, st *store.Store, pat *patterns.Compiled, workerFn func(*http.Request) string) *Handler {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	// DisableCompression keeps upstream Content-Encoding intact so we do not
	// rewrite lengths. Keep HTTP/2 to origin (registry.npmjs.org ALPN is
	// h2-only-enough that ForceAttemptHTTP2=false reads SETTINGS as HTTP/1.1
	// and 502s). Rewrite the response to HTTP/1.1 before writing it onto the
	// MITM tunnel.
	tr.Proxy = nil
	tr.DisableCompression = true
	tr.ResponseHeaderTimeout = headerTimeout
	return &Handler{
		CA:       ca,
		Store:    st,
		Patterns: pat,
		Worker:   workerFn,
		client: &http.Client{
			Transport: tr,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

// serveMITM terminates TLS with a leaf for host and serves decrypted
// HTTP/1.1 from the client over the tunnel, running the full scan/decision/
// substitution pipeline per request. br must be a reader over the connection
// that may already hold buffered ClientHello bytes.
func (h *Handler) serveMITM(conn net.Conn, br *bufio.Reader, host string, r *http.Request) {
	// Server-side TLS with a leaf signed for this hostname.
	leaf, err := h.CA.Leaf(host)
	if err != nil {
		log.Printf("proxy: leaf for %s: %v", host, err)
		return
	}
	tlsConn := tls.Server(&hijackedConn{Conn: conn, br: br}, &tls.Config{
		Certificates: []tls.Certificate{leaf.toTLS()},
		MinVersion:   tls.VersionTLS12,
		NextProtos:   []string{"http/1.1"},
	})
	if err := tlsConn.Handshake(); err != nil {
		log.Printf("proxy: tls handshake for %s: %v", host, err)
		return
	}
	defer tlsConn.Close()

	// Serve decrypted HTTP/1.1 from the client over the tunnel.
	brr := bufio.NewReader(tlsConn)
	for {
		req, err := http.ReadRequest(brr)
		if err != nil {
			return // EOF or client closed
		}
		req.URL.Scheme = "https"
		req.URL.Host = host
		if req.Host == "" {
			req.Host = host
		}
		// http.ReadRequest does not populate RemoteAddr. r carries the worker's
		// origin IP so worker-identity lookup (IP→slug) works for MITM'd requests.
		req.RemoteAddr = r.RemoteAddr
		// streamTimeout covers headers + streamed body. headerTimeout on
		// the transport still fails fast when the origin never sends headers.
		fwdCtx, cancel := context.WithTimeout(context.Background(), streamTimeout)
		resp, keepAlive := h.process(fwdCtx, req, "https")
		writeErr := writeTunnelResponse(tlsConn, resp)
		_ = resp.Body.Close()
		cancel()
		if writeErr != nil {
			return
		}
		if resp.Close || !keepAlive {
			return
		}
	}
}

// hostNeedsInterception reports whether any configured rule, domain policy,
// or enabled exception references host. Only such hosts are MITM'd; every
// other host is spliced as raw TCP (real end-to-end TLS, no inspection).
func (h *Handler) hostNeedsInterception(host string) bool {
	for _, r := range recRules(h.Store) {
		if r.Host == "*" || strings.EqualFold(r.Host, host) {
			return true
		}
	}
	if _, ok := recDomains(h.Store)[strings.ToLower(host)]; ok {
		return true
	}
	for _, e := range recExceptions(h.Store) {
		if !e.Enabled {
			continue
		}
		if e.HostRegex == "" {
			return true // host-wide exception: applies to every host
		}
		re, err := regexp.Compile(e.HostRegex)
		if err != nil {
			continue
		}
		if re.MatchString(host) {
			return true
		}
	}
	return false
}

// splice bridges br (the client side, which may already hold buffered bytes
// such as a TLS ClientHello) to the origin at addrs:port as a raw byte
// tunnel — no TLS termination, no scanning, no CA. Either leg ending tears
// down both; this returns once the splice finishes.
func (h *Handler) splice(conn net.Conn, br *bufio.Reader, host string, port int, addrs []net.IPAddr) {
	// Dial the origin, reusing the already-verified (non-internal) addresses.
	var up net.Conn
	var dialErr error
	for _, a := range addrs {
		na, aok := netip.AddrFromSlice(a.IP)
		if !aok {
			continue
		}
		ap := netip.AddrPortFrom(na, uint16(port))
		u, uerr := net.Dial("tcp", ap.String())
		if uerr != nil {
			dialErr = uerr
			continue
		}
		up = u
		dialErr = nil
		break
	}
	if dialErr != nil {
		log.Printf("proxy: splice: dial %s: %v", host, dialErr)
		_ = conn.Close()
		return
	}
	defer up.Close()

	// Bidirectional splice. The client leg MUST start from the buffered
	// reader (it may hold a ClientHello already read from conn), never the
	// raw conn.
	done := make(chan struct{})
	go func() {
		_, _ = io.CopyBuffer(up, br, make([]byte, 65536))
		_ = up.Close()
		close(done)
	}()
	go func() {
		_, _ = io.CopyBuffer(conn, up, make([]byte, 65536))
		_ = up.Close()
		_ = conn.Close()
		close(done)
	}()
	<-done
}

// resolveExternalHost resolves host and refuses internal/link-local targets
// (metadata-style attacks). Returns the verified external addresses.
func (h *Handler) resolveExternalHost(host string) ([]net.IPAddr, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, err
	}
	for _, a := range addrs {
		if a.IP.IsLoopback() || a.IP.IsPrivate() || a.IP.IsLinkLocalUnicast() || a.IP.IsUnspecified() {
			return nil, fmt.Errorf("internal target denied: %s", host)
		}
	}
	return addrs, nil
}

// handleTransparentConn handles one connection accepted on the intercept
// listener: sniff TLS SNI, replay the buffered ClientHello, then MITM when
// the host is ruled or splice raw TCP otherwise. No SO_ORIGINAL_DST needed —
// the gateway dials the origin itself from SNI.
func (h *Handler) handleTransparentConn(conn net.Conn, remote string) {
	defer conn.Close()
	sni, hello, err := readClientHelloSNI(conn)
	if err != nil || sni == "" {
		log.Printf("proxy: transparent: no SNI (%v)", err)
		return
	}
	r := &http.Request{RemoteAddr: remote}
	if blocked, slug := h.workerInternetBlocked(r); blocked {
		h.Store.AppendLog(store.Record{
			ID: recID(), Worker: h.workerID(r), Slug: slug, Ts: time.Now().UTC(),
			Method: "HTTPS", Scheme: "https", Host: sni, Path: "/",
			Action: string(model.ActionBlock), Status: http.StatusForbidden,
			Reasons: []string{"internet-disabled"},
		})
		return
	}
	addrs, rerr := h.resolveExternalHost(sni)
	if rerr != nil {
		log.Printf("proxy: transparent: %v", rerr)
		return
	}
	br := bufio.NewReader(&prependReader{Conn: conn, Prefix: hello})
	if !h.hostNeedsInterception(sni) {
		h.splice(conn, br, sni, 443, addrs)
		return
	}
	h.serveMITM(conn, br, sni, r)
}

// ServeTransparentLoop accepts raw TLS connections on addr (vpc :443 intercept
// target) and dispatches each to handleTransparentConn.
func (h *Handler) ServeTransparentLoop(addr string) error {
	la, err := net.ResolveTCPAddr("tcp", addr)
	if err != nil {
		return err
	}
	l, err := net.ListenTCP("tcp", la)
	if err != nil {
		return err
	}
	defer l.Close()
	log.Printf("proxy: transparent REDIRECT listener on %s", addr)
	for {
		c, err := l.Accept()
		if err != nil {
			return err
		}
		cc := c // explicit copy: the goroutine must not race the next Accept
		go func() {
			h.handleTransparentConn(cc, remoteOf(cc))
		}()
	}
}

// remoteOf renders a connection's peer as "ip:port" for worker-identity
// lookup, mirroring how the HTTP server populates RemoteAddr.
func remoteOf(c net.Conn) string {
	return c.RemoteAddr().String()
}

// readFull blocks until n bytes are read from conn, or the connection
// closes/errors.
func readFull(conn net.Conn, n int) ([]byte, error) {
	out := make([]byte, n)
	got := 0
	for got < n {
		m, err := conn.Read(out[got:])
		if err != nil {
			return nil, err
		}
		if m == 0 {
			return nil, fmt.Errorf("EOF reading TLS record")
		}
		got += m
	}
	return out, nil
}

// readClientHelloSNI reads the first TLS record, extracts the RFC 4366
// server_name (SNI), and returns it with the FULL record bytes so callers
// can replay them into the MITM or splice paths.
func readClientHelloSNI(conn net.Conn) (string, []byte, error) {
	hdr, err := readFull(conn, 5)
	if err != nil {
		return "", nil, err
	}
	if len(hdr) != 5 || hdr[0] != 0x16 {
		return "", nil, fmt.Errorf("not a TLS handshake record")
	}
	hlen := int(hdr[3])*256 + int(hdr[4])
	if hlen <= 0 || hlen > 1<<14 {
		return "", nil, fmt.Errorf("bad ClientHello length %d", hlen)
	}
	body, err := readFull(conn, hlen)
	if err != nil {
		return "", nil, err
	}
	sni, _ := parseClientHelloSNI(body)
	hello := make([]byte, 5+hlen)
	for i := 0; i < 5; i++ {
		hello[i] = hdr[i]
	}
	for i := 0; i < hlen; i++ {
		hello[5+i] = body[i]
	}
	return sni, hello, nil
}

// parseClientHelloSNI walks a ClientHello handshake body (after the 5-byte
// record header) and returns the first server_name entry. ok=false when the
// message is not a ClientHello or has no (parseable) SNI extension.
func parseClientHelloSNI(b []byte) (string, bool) {
	// handshake header: type(1) length(3) version(2) random(32)
	if len(b) < 4+2+32+1 {
		return "", false
	}
	if b[0] != 0x01 {
		return "", false
	}
	i := 4 + 2 + 32 // skip type+len(4), version(2), random(32)
	// session id
	if 1 > len(b)-i || int(b[i]) > len(b)-i-1 {
		return "", false
	}
	i += 1 + int(b[i])
	// cipher suites: len(2) + list
	if 2 > len(b)-i {
		return "", false
	}
	clen := int(b[i])*256 + int(b[i+1])
	i += 2 + clen
	// compression: len(1) + list
	if 1 > len(b)-i {
		return "", false
	}
	i += 1 + int(b[i])
	// extensions (optional)
	if 2 > len(b)-i {
		return "", false
	}
	exlen := int(b[i])*256 + int(b[i+1])
	i += 2
	if exlen > len(b)-i {
		return "", false
	}
	end := i + exlen
	for i+4 <= end {
		etype := int(b[i])*256 + int(b[i+1])
		eelen := int(b[i+2])*256 + int(b[i+3])
		i += 4
		if i+eelen > end {
			return "", false
		}
		if etype == 0x0000 { // server_name
			// list_len(2) then one or more (type(1) len(2) name)
			j := i + 2
			if j+3 > i+eelen || b[j] != 0x00 {
				break
			}
			nlen := int(b[j+1])*256 + int(b[j+2])
			if nlen == 0 || j+3+nlen > i+eelen {
				break
			}
			return string(b[j+3 : j+3+nlen]), true
		}
		i += eelen
	}
	return "", false
}

// prependReader yields Prefix bytes first, then reads from Conn — used to
// replay a buffered TLS ClientHello after the SNI was sniffed off the wire.
type prependReader struct {
	net.Conn
	Prefix []byte
	Off    int
}

func (c *prependReader) Read(p []byte) (int, error) {
	if c.Off < len(c.Prefix) {
		n := len(p)
		if len(c.Prefix)-c.Off < n {
			n = len(c.Prefix) - c.Off
		}
		for j := 0; j < n; j++ {
			p[j] = c.Prefix[c.Off+j]
		}
		c.Off += n
		return n, nil
	}
	return c.Conn.Read(p)
}

// hijackedConn reads leftover bytes from a bufio.Reader before the underlying
// connection. The intercepted ClientHello sits in br after SNI sniffing.
type hijackedConn struct {
	net.Conn
	br *bufio.Reader
}

func (c *hijackedConn) Read(p []byte) (int, error) {
	return c.br.Read(p)
}

// Replay re-runs the full policy on a captured request and forwards it. This
// is the "add a secret, then retry" path — the policy is re-evaluated fresh;
// a rule added while the request sat blocked is never trusted blindly.
func (h *Handler) Replay(ctx context.Context, rec store.Record) (*http.Response, model.Decision, error) {
	if rec.Capture == nil {
		return nil, model.Decision{}, fmt.Errorf("no capture")
	}
	c := rec.Capture
	req, err := http.NewRequestWithContext(ctx, c.Method, c.URL, bytes.NewReader(c.Body))
	if err != nil {
		return nil, model.Decision{}, err
	}
	for k, vv := range c.Headers {
		if strings.EqualFold(k, "Host") {
			req.Host = vv[0]
			continue
		}
		for _, v := range vv {
			req.Header.Add(k, v)
		}
	}
	body, _ := io.ReadAll(req.Body)
	req.Body = io.NopCloser(bytes.NewReader(body))

	dec := h.decide(req, rec.Scheme, body)
	if dec.Action != model.ActionAllow && dec.Action != model.ActionSubstitute {
		return nil, dec, fmt.Errorf("still blocked: %v", dec.Reasons)
	}
	if dec.Action == model.ActionSubstitute {
		applySubstitution(req, body, dec, h.Store, scanner.ScanRequest(req, scanner.Options{
			Worker: h.workerID(req), Secrets: knownSecrets(h.Store), Patterns: h.Patterns,
		}))
	}
	out, err := h.forward(ctx, req, rec.Scheme)
	return out, dec, err
}

// decide is the scan + policy step shared by process and Replay.
func (h *Handler) decide(req *http.Request, scheme string, body []byte) model.Decision {
	scan := scanner.ScanRequest(req, scanner.Options{
		Worker: h.workerID(req), Secrets: knownSecrets(h.Store), Patterns: h.Patterns,
	})
	return policy.Decide(policy.Input{
		Method: req.Method, Schema: scheme, Host: req.URL.Hostname(), Path: req.URL.Path,
		Body: string(body), Scan: scan,
		SecretByPlaceholder: placeholderMap(h.Store),
		Rules:               recRules(h.Store),
		Domains:             recDomains(h.Store),
		Exceptions:          recExceptions(h.Store),
		Worker:              h.workerID(req),
	})
}

// process is the core: scan → decide → substitute/block → forward → record.
func (h *Handler) process(ctx context.Context, req *http.Request, scheme string) (*http.Response, bool) {
	body, _ := io.ReadAll(req.Body)
	req.Body = io.NopCloser(bytes.NewReader(body))

	// Internet kill-switch: worker toggled off can't egress (plain HTTP too).
	if blocked, slug := h.workerInternetBlocked(req); blocked {
		reqID := newID()
		rec := store.Record{
			ID: recID(), Worker: h.workerID(req), Slug: slug, Ts: time.Now().UTC(),
			Method: req.Method, Scheme: scheme, Host: req.URL.Hostname(),
			Path: req.URL.RequestURI(), RequestID: reqID,
			Action: string(model.ActionBlock), Status: http.StatusForbidden,
			Reasons: []string{"internet-disabled"},
		}
		h.Store.AppendLog(rec)
		return &http.Response{
			Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1,
			StatusCode: http.StatusForbidden, Status: "403 Forbidden",
			Header:  http.Header{"Connection": {"close"}},
			Body:    io.NopCloser(bytes.NewReader(nil)),
			Request: req,
			Close:   true,
		}, false
	}

	dec := h.decide(req, scheme, body)
	scan := scanner.ScanRequest(req, scanner.Options{
		Worker: h.workerID(req), Secrets: knownSecrets(h.Store), Patterns: h.Patterns,
	})

	reqID := newID()
	wid := h.workerID(req)
	rec := store.Record{
		ID:        recID(),
		Worker:    wid,
		Slug:      wid,
		Ts:        time.Now().UTC(),
		Method:    req.Method,
		Scheme:    scheme,
		Host:      req.URL.Hostname(),
		Path:      req.URL.RequestURI(),
		RequestID: reqID,
	}
	rec.Findings = toStoreFindings(scan)

	switch dec.Action {
	case model.ActionBlock:
		rec.Action = string(model.ActionBlock)
		rec.Status = http.StatusProxyAuthRequired
		rec.Reasons = toStrings(dec.Reasons)
		rec.Retryable = true
		rec.Capture = capture(req, body)
		h.Store.AppendLog(rec)
		resp := &http.Response{
			Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1,
			StatusCode: http.StatusProxyAuthRequired,
			Status:     "407 Proxy Authentication Required",
			Header: http.Header{
				"X-Piso-Request-Id": {reqID},
				"X-Piso-Reason":     {string(dec.Reasons[0])},
				"Content-Length":    {"0"},
				"Connection":        {"close"},
			},
			Body:          io.NopCloser(bytes.NewReader(nil)),
			ContentLength: 0,
			Request:       req,
			Close:         true,
		}
		return resp, false

	case model.ActionSubstitute:
		applySubstitution(req, body, dec, h.Store, scan)
		rec.Action = string(model.ActionSubstitute)
		rec.Status = http.StatusOK
		rec.Reasons = toStrings(dec.Reasons)

	case model.ActionAllow:
		rec.Action = string(model.ActionAllow)
		rec.Status = http.StatusOK
		rec.Reasons = toStrings(dec.Reasons)
	}

	out, err := h.forward(ctx, req, scheme)
	if err != nil {
		log.Printf("proxy: forward to %s failed: %v", req.URL.Host, err)
		rec.Action = string(model.ActionBlock)
		rec.Status = http.StatusBadGateway
		rec.Reasons = append(rec.Reasons, "upstream-error")
		h.Store.AppendLog(rec)
		return &http.Response{
			Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1,
			StatusCode: http.StatusBadGateway,
			Status:     "502 Bad Gateway",
			Header:     http.Header{"Connection": {"close"}},
			Body:       io.NopCloser(bytes.NewReader(nil)),
			Request:    req,
			Close:      true,
		}, false
	}
	prepareTunnelResponse(out)
	out.Request = req
	rec.Retryable = false
	h.Store.AppendLog(rec)
	return out, true
}

// prepareTunnelResponse rewrites an upstream response so it is valid HTTP/1.1
// on the MITM tunnel. HTTP/2 origins set ProtoMajor=2; Alt-Svc tells the
// client to switch to h2/h3 on the next request — both break this proxy.
func prepareTunnelResponse(resp *http.Response) {
	if resp == nil {
		return
	}
	resp.Proto = "HTTP/1.1"
	resp.ProtoMajor = 1
	resp.ProtoMinor = 1
	if resp.Header == nil {
		resp.Header = make(http.Header)
	}
	for _, h := range []string{
		"Alt-Svc",
		"Alt-Used",
		"HTTP2-Settings",
		"Upgrade",
		"Keep-Alive",
		"Proxy-Connection",
	} {
		resp.Header.Del(h)
	}
}

// writeTunnelResponse writes a clean HTTP/1.1 response onto the MITM tunnel.
// Origin bodies are streamed with chunked encoding so chat completions are
// not held until EOF (buffering hid the first token and raced streamTimeout).
// HTTP/2 Content-Length is ignored: origins often advertise the wrong length.
// The caller still closes the body.
func writeTunnelResponse(w io.Writer, resp *http.Response) error {
	if resp == nil {
		return fmt.Errorf("nil response")
	}
	prepareTunnelResponse(resp)

	status := resp.StatusCode
	if status == 0 {
		status = http.StatusOK
	}
	text := http.StatusText(status)
	if text == "" {
		text = "OK"
	}

	hdr := make(http.Header)
	if resp.Header != nil {
		hdr = resp.Header.Clone()
	}
	for _, h := range []string{"Content-Length", "Transfer-Encoding"} {
		hdr.Del(h)
	}

	noBody := !responseHasBody(resp)
	if noBody {
		hdr.Set("Content-Length", "0")
	} else {
		hdr.Set("Transfer-Encoding", "chunked")
	}

	if _, err := fmt.Fprintf(w, "HTTP/1.1 %03d %s\r\n", status, text); err != nil {
		return err
	}
	if err := hdr.Write(w); err != nil {
		return err
	}
	if _, err := io.WriteString(w, "\r\n"); err != nil {
		return err
	}
	flushWriter(w)
	if noBody {
		return nil
	}

	cw := httputil.NewChunkedWriter(w)
	if _, err := io.CopyBuffer(cw, resp.Body, make([]byte, streamCopyBuf)); err != nil {
		_ = cw.Close()
		return err
	}
	if err := cw.Close(); err != nil {
		return err
	}
	_, err := io.WriteString(w, "\r\n")
	if err != nil {
		return err
	}
	flushWriter(w)
	return nil
}

// responseHasBody reports whether the response carries an entity body on the
// wire. Content-Length is ignored: HTTP/2 origins often advertise 0 or a
// stale value while still sending bytes.
func responseHasBody(resp *http.Response) bool {
	if resp == nil || resp.Body == nil {
		return false
	}
	if resp.StatusCode == http.StatusNoContent || resp.StatusCode == http.StatusNotModified {
		return false
	}
	if resp.Request != nil && resp.Request.Method == http.MethodHead {
		return false
	}
	return true
}

// flushWriter flushes w when it exposes Flush, so SSE tokens leave the
// gateway instead of sitting in a bufio buffer.
func flushWriter(w io.Writer) {
	switch f := w.(type) {
	case interface{ Flush() error }:
		_ = f.Flush()
	case interface{ Flush() }:
		f.Flush()
	}
}

// forward replays the (possibly substituted) request upstream. The body is
// already in memory (process read it for scanning), so GetBody is set and a
// dead HTTP/2 connection can be retried once — Go will not retry POST itself.
func (h *Handler) forward(ctx context.Context, req *http.Request, scheme string) (*http.Response, error) {
	target := *req.URL
	target.Scheme = scheme
	target.Host = req.URL.Host
	outReq := req.Clone(ctx)
	outReq.URL = &target
	outReq.RequestURI = ""
	if err := ensureGetBody(outReq); err != nil {
		return nil, err
	}

	resp, err := h.client.Do(outReq)
	if err == nil || !isRetryableUpstream(err) || outReq.GetBody == nil {
		return resp, err
	}

	body, getErr := outReq.GetBody()
	if getErr != nil {
		return nil, err
	}
	retry := outReq.Clone(ctx)
	retry.Body = body
	retry.GetBody = outReq.GetBody
	log.Printf("proxy: retrying %s %s after dead upstream conn: %v", retry.Method, retry.URL.Host, err)
	return h.client.Do(retry)
}

// ensureGetBody snapshots req.Body so the transport (and forward's own retry)
// can rewind POST/PUT/PATCH after a dead connection. No-op when GetBody is
// already set or there is no body.
func ensureGetBody(req *http.Request) error {
	if req == nil {
		return fmt.Errorf("nil request")
	}
	if req.GetBody != nil {
		return nil
	}
	if req.Body == nil || req.Body == http.NoBody {
		return nil
	}
	b, err := io.ReadAll(req.Body)
	_ = req.Body.Close()
	if err != nil {
		return err
	}
	req.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(b)), nil
	}
	req.Body = io.NopCloser(bytes.NewReader(b))
	req.ContentLength = int64(len(b))
	return nil
}

// isRetryableUpstream reports whether err is a dead/idle HTTP/2 (or TCP)
// connection, safe to retry once because the body is rewindable. Timeouts
// and cancellations are not retried: those are a slow or abandoned origin.
func isRetryableUpstream(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return false
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return true
	}
	msg := strings.ToLower(err.Error())
	for _, needle := range []string{
		"http2: server sent goaway",
		"http2: client connection lost",
		"http2: client conn not usable",
		"http2: transport received server's graceful shutdown goaway",
		"refused stream",
		"connection reset by peer",
		"broken pipe",
		"use of closed network connection",
		"server closed idle connection",
		"http2: connection error",
	} {
		if strings.Contains(msg, needle) {
			return true
		}
	}
	return false
}

// applySubstitution replaces placeholder tokens with real values in headers
// and non-transcript body fields. The LLM messages[] transcript is never
// rewritten: a whole-body ReplaceAll would leak the real secret to the model
// whenever chat text mentioned the same piso_ token (including the provider
// key sitting in Authorization). Pass the original body bytes from process.
func applySubstitution(req *http.Request, origBody []byte, dec model.Decision, st *store.Store, scan scanner.Result) {
	pm := placeholderMap(st)
	if len(dec.Substituted) == 0 {
		return
	}
	sub := map[string]string{}
	for _, tok := range dec.Substituted {
		if s, ok := pm[tok]; ok {
			sub[tok] = s.Value
		}
	}
	if len(sub) == 0 {
		return
	}
	for name, vals := range req.Header {
		auth := http.CanonicalHeaderKey(name) == "Authorization"
		for i, v := range vals {
			nv := v
			if auth {
				// Authorization: Basic base64-wraps the payload, so a literal
				// ReplaceAll on the raw value never reaches the placeholder.
				// Decode, substitute inside the payload, re-encode.
				if payload, ok := scanner.BasicAuthPayload(v); ok {
					nd := payload
					for tok, real := range sub {
						nd = strings.ReplaceAll(nd, tok, real)
					}
					if nd != payload {
						vals[i] = "Basic " + base64.StdEncoding.EncodeToString([]byte(nd))
						continue
					}
				}
			}
			for tok, real := range sub {
				nv = strings.ReplaceAll(nv, tok, real)
			}
			vals[i] = nv
		}
	}
	if len(origBody) == 0 {
		return
	}
	nb := origBody
	if rewritten, ok := substituteJSONSkippingChat(origBody, sub); ok {
		nb = rewritten
	} else {
		for tok, real := range sub {
			nb = bytes.ReplaceAll(nb, []byte(tok), []byte(real))
		}
	}
	req.Body = io.NopCloser(bytes.NewReader(nb))
	req.ContentLength = int64(len(nb))
	req.Header.Set("Content-Length", fmt.Sprint(len(nb)))
}

// substituteJSONSkippingChat rewrites string values in a JSON body except
// those under messages[]. Non-JSON input returns ok=false.
func substituteJSONSkippingChat(body []byte, sub map[string]string) ([]byte, bool) {
	// Only attempt the JSON rewrite when the body actually is JSON. The JSON
	// decoder decodes a single leading value and ignores trailing input, so
	// running it on arbitrary binary (e.g. a git pkt-line body whose 4-hex
	// length prefix "0014…" is accepted as the number 0) would silently
	// truncate the payload to that one decoded token. Gate on the first
	// non-space byte being '{' or '[' and require the body to be exactly one
	// JSON value before touching it — anything else falls through to the
	// non-JSON caller path and is forwarded byte for byte.
	t := bytes.TrimSpace(body)
	if len(t) == 0 || (t[0] != '{' && t[0] != '[') {
		return nil, false
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var raw any
	if err := dec.Decode(&raw); err != nil {
		return nil, false
	}
	// Guarantee the body was exactly one JSON value (reject trailing data).
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return nil, false
	}
	rewritten := rewriteJSONSkippingChat(raw, "", sub)
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(rewritten); err != nil {
		return nil, false
	}
	out := bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
	return out, true
}

func rewriteJSONSkippingChat(v any, path string, sub map[string]string) any {
	switch t := v.(type) {
	case map[string]any:
		for k, cv := range t {
			p := k
			if path != "" {
				p = path + "." + k
			}
			t[k] = rewriteJSONSkippingChat(cv, p, sub)
		}
		return t
	case []any:
		for i, cv := range t {
			t[i] = rewriteJSONSkippingChat(cv, path+"["+strconv.Itoa(i)+"]", sub)
		}
		return t
	case string:
		if scanner.IsChatContent("json-body", path) {
			return t
		}
		for tok, real := range sub {
			t = strings.ReplaceAll(t, tok, real)
		}
		return t
	default:
		return t
	}
}

// ---- helpers ----

func knownSecrets(st *store.Store) []scanner.KnownSecret {
	recs := st.Secrets()
	out := make([]scanner.KnownSecret, 0, len(recs))
	for _, r := range recs {
		if r.Value != "" {
			out = append(out, scanner.KnownSecret{ID: r.ID, Value: r.Value})
		}
	}
	return out
}

func placeholderMap(st *store.Store) map[string]model.Secret {
	recs := st.Secrets()
	out := make(map[string]model.Secret, len(recs))
	for _, r := range recs {
		if r.Placeholder != "" {
			out[r.Placeholder] = model.Secret{
				ID: r.ID, Name: r.Name, Placeholder: r.Placeholder, Value: r.Value,
			}
		}
	}
	return out
}

func recRules(st *store.Store) []model.Rule {
	rs := st.Rules()
	out := make([]model.Rule, 0, len(rs))
	for _, r := range rs {
		out = append(out, model.Rule{ID: r.ID, SecretID: r.SecretID, Host: r.Host, Placeholder: r.Placeholder, Note: r.Note})
	}
	return out
}

func recDomains(st *store.Store) map[string]model.DomainPolicy {
	ds := st.Domains()
	out := make(map[string]model.DomainPolicy, len(ds))
	for _, d := range ds {
		out[strings.ToLower(d.Host)] = model.DomainPolicy{Host: d.Host, Deny: d.Deny, NeedsRule: d.NeedsRule, Note: d.Note}
	}
	return out
}

func recExceptions(st *store.Store) []model.Exception {
	es := st.Exceptions()
	out := make([]model.Exception, 0, len(es))
	for _, e := range es {
		out = append(out, model.Exception{
			ID: e.ID, Note: e.Note, HostRegex: e.HostRegex, PathRegex: e.PathRegex,
			ContentRegex: e.ContentRegex, Placeholder: e.Placeholder,
			PatternID: e.PatternID, Enabled: e.Enabled,
		})
	}
	return out
}

func toStoreFindings(scan scanner.Result) []store.Finding {
	var out []store.Finding
	add := func(f model.Finding) {
		sf := store.Finding{Kind: string(f.Kind), PatternID: f.PatternID, SecretID: f.SecretID, Token: f.Token, Location: f.Location, Field: f.Field}
		if sf.Token != "" && len(sf.Token) > 24 {
			sf.Token = sf.Token[:12] + "…"
		}
		out = append(out, sf)
	}
	for _, f := range scan.RealSecrets {
		add(f)
	}
	for _, f := range scan.PatternHits {
		add(f)
	}
	for _, f := range scan.Placeholders {
		add(f)
	}
	return out
}

func capture(req *http.Request, body []byte) *store.Capture {
	hdr := make(map[string][]string, len(req.Header))
	for k, v := range req.Header {
		hdr[k] = append([]string(nil), v...)
	}
	return &store.Capture{
		Method:  req.Method,
		URL:     req.URL.String(),
		Headers: hdr,
		Body:    append([]byte(nil), body...),
	}
}

func (h *Handler) workerID(r *http.Request) string {
	if h.Worker != nil {
		if w := h.Worker(r); w != "" {
			return w
		}
	}
	return "worker"
}

// workerInternetBlocked reports whether the request's origin worker has the
// internet kill-switch enabled. It resolves the origin IP via the proxy's
// worker function (identity), then checks the registry flag. identity empty
// → not blocked (unknown worker can't be toggled).
func (h *Handler) workerInternetBlocked(r *http.Request) (blocked bool, slug string) {
	if h.Worker == nil {
		return false, ""
	}
	identity := h.workerID(r)
	if identity == "" || identity == "worker" {
		return false, ""
	}
	// workerID returns the slug when known; look up by that slug's name is
	// ambiguous — instead resolve by origin IP to get the full registry entry.
	// The proxy's worker fn returns the slug; find the record by it after all
	// (Slug is unique per worker).
	if rec, ok := h.Store.WorkerBySlug(identity); ok {
		return rec.InternetDisabled, rec.Slug
	}
	return false, ""
}

func copyHeader(dst, src http.Header) {
	for k, vv := range src {
		for _, v := range vv {
			dst.Add(k, v)
		}
	}
	dst.Del("Proxy-Authenticate")
	dst.Del("Proxy-Authorization")
}

func toStrings(rs []model.Reason) []string {
	out := make([]string, 0, len(rs))
	for _, r := range rs {
		out = append(out, string(r))
	}
	return out
}
