package proxy

import (
	"bytes"
	"net"
	"strings"
	"testing"
)

// --- synthetic TLS ClientHello builder -------------------------------------

func hb1(w *bytes.Buffer, v int) error {
	_, err := w.Write([]byte{byte(v & 0xff)})
	return err
}

func hb16(w *bytes.Buffer, v int) error {
	_, err := w.Write([]byte{byte((v >> 8) & 0xff), byte(v & 0xff)})
	return err
}

func hb24(w *bytes.Buffer, v int) error {
	_, err := w.Write([]byte{byte((v >> 16) & 0xff), byte((v >> 8) & 0xff), byte(v & 0xff)})
	return err
}

// clientHelloRecord builds a minimal TLS 1.2 ClientHello record. When sni is
// non-empty it carries an RFC 4366 server_name extension, otherwise a dummy.
func clientHelloRecord(sni string) []byte {
	var tail bytes.Buffer
	_ = hb16(&tail, 0x0303) // client_version TLS 1.2
	random := make([]byte, 32)
	for i := 0; i < 32; i++ {
		random[i] = byte(i)
	}
	_, _ = tail.Write(random)
	_ = hb1(&tail, 0)  // no session id
	_ = hb16(&tail, 2) // one cipher suite: TLS_AES_128_GCM_SHA256
	_ = hb16(&tail, 0x1301)
	_ = hb1(&tail, 1) // one compression method: null
	_ = hb1(&tail, 0x00)

	var exts bytes.Buffer
	etype := 0xff01 // dummy extension
	if sni != "" {
		etype = 0x0000 // server_name
		_ = hb16(&exts, etype)
		listLen := 3 + len(sni)
		_ = hb16(&exts, 2+listLen)
		_ = hb16(&exts, listLen)
		_ = hb1(&exts, 0x00) // name_type host_name
		_ = hb16(&exts, len(sni))
		_, _ = exts.Write([]byte(sni))
	} else {
		_ = hb16(&exts, etype)
		_ = hb16(&exts, 0)
	}

	_ = hb16(&tail, exts.Len())
	_, _ = tail.Write(exts.Bytes())

	var body bytes.Buffer
	_ = hb1(&body, 0x01)        // handshake type ClientHello
	_ = hb24(&body, tail.Len()) // handshake length
	_, _ = body.Write(tail.Bytes())

	var rec bytes.Buffer
	_ = hb1(&rec, 0x16) // record type handshake
	_ = hb16(&rec, 0x0301)
	_ = hb16(&rec, body.Len())
	_, _ = rec.Write(body.Bytes())
	return rec.Bytes()
}

// --- tests ------------------------------------------------------------------

func TestReadClientHelloSNIWithSNI(t *testing.T) {
	hello := clientHelloRecord("github.com")
	c1, c2 := net.Pipe()
	defer c1.Close()
	defer c2.Close()
	done := make(chan struct{})
	go func() {
		_, _ = c2.Write(hello)
		_ = c2.Close()
		close(done)
	}()
	sni, got, err := readClientHelloSNI(c1)
	<-done
	if err != nil {
		t.Fatalf("readClientHelloSNI: %v", err)
	}
	if sni != "github.com" {
		t.Fatalf("want SNI github.com, got %q", sni)
	}
	if len(got) != len(hello) || strings.Compare(string(got), string(hello)) != 0 {
		t.Fatalf("replayed record mismatch: %d bytes vs %d", len(got), len(hello))
	}
}

func TestReadClientHelloSNIWithoutSNI(t *testing.T) {
	hello := clientHelloRecord("")
	c1, c2 := net.Pipe()
	defer c1.Close()
	defer c2.Close()
	done := make(chan struct{})
	go func() {
		_, _ = c2.Write(hello)
		_ = c2.Close()
		close(done)
	}()
	sni, _, err := readClientHelloSNI(c1)
	<-done
	if err != nil {
		t.Fatalf("readClientHelloSNI: %v", err)
	}
	if sni != "" {
		t.Fatalf("want no SNI, got %q", sni)
	}
}

func TestReadClientHelloSNIRejectsNonTLS(t *testing.T) {
	c1, c2 := net.Pipe()
	defer c1.Close()
	defer c2.Close()
	done := make(chan struct{})
	go func() {
		// Exactly the 5-byte record header: net.Pipe Write blocks until the
		// reader consumes the bytes, and we stop reading after the header.
		_, _ = c2.Write([]byte("GET /"))
		_ = c2.Close()
		close(done)
	}()
	_, _, err := readClientHelloSNI(c1)
	<-done
	if err == nil {
		t.Fatal("plain HTTP must be rejected, not parsed")
	}
}

func TestParseClientHelloSNITruncated(t *testing.T) {
	hello := clientHelloRecord("example.com")
	if _, ok := parseClientHelloSNI(hello[5 : len(hello)-3]); ok {
		t.Fatal("truncated ClientHello must fail to parse")
	}
	if _, ok := parseClientHelloSNI([]byte("garbage")); ok {
		t.Fatal("garbage must fail to parse")
	}
}

func TestPrependReaderReplaysPrefix(t *testing.T) {
	c1, c2 := net.Pipe()
	defer c1.Close()
	defer c2.Close()
	pr := &prependReader{Conn: c1, Prefix: []byte("AB")}
	rest := make([]byte, 16)
	if n, _ := pr.Read(rest[:0]); n != 0 {
		t.Fatalf("zero-length read: %d", n)
	}
	b := make([]byte, 1)
	if n, err := pr.Read(b); err != nil || n != 1 || b[0] != 'A' {
		t.Fatalf("prefix A: %d %v", n, err)
	}
	if n, err := pr.Read(b); err != nil || n != 1 || b[0] != 'B' {
		t.Fatalf("prefix B: %d %v", n, err)
	}
	done := make(chan struct{})
	go func() {
		_, _ = c2.Write([]byte("XY"))
		_ = c2.Close()
		close(done)
	}()
	if n, err := pr.Read(b); err != nil || n != 1 || b[0] != 'X' {
		t.Fatalf("conn X: %d %v", n, err)
	}
	if n, err := pr.Read(b); err != nil || n != 1 || b[0] != 'Y' {
		t.Fatalf("conn Y: %d %v", n, err)
	}
	<-done
}
