package core

// Regression test for scripts/go2rtc-writebuffer-recover.patch. Copied into
// pkg/core/ by scripts/build_go2rtc.sh and run before every build, so a
// future go2rtc version bump can't silently drop the patch's effect.
//
// Fakes the exact failure mode observed in production and in upstream
// issue AlexxIT/go2rtc#1261: the underlying io.Writer (an in-flight
// net/http ResponseWriter in real usage) panics on Write because the HTTP
// handler goroutine serving it has already torn it down. Before the patch
// this crashed the whole process; after it, Write() must return a
// contained error instead.

import "testing"

type panicWriter struct{}

func (panicWriter) Write(p []byte) (int, error) {
	panic("simulated nil pointer dereference (net/http response torn down)")
}

func TestWriteBufferRecoversFromWriterPanic(t *testing.T) {
	wb := NewWriteBuffer(panicWriter{})

	n, err := wb.Write([]byte("hello"))
	if err == nil {
		t.Fatal("expected a contained error, got nil -- panic was not recovered")
	}
	if n != 0 {
		t.Fatalf("expected n=0 on a recovered panic, got %d", n)
	}

	// The error must latch (w.err), so a second write on the same broken
	// consumer fails cleanly instead of re-attempting the panicking write.
	if _, err2 := wb.Write([]byte("world")); err2 == nil {
		t.Fatal("expected a latched error on second write, got nil")
	}
}
