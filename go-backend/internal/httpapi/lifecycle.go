package httpapi

import (
	"context"
	"errors"
	"net"
	"net/http"
	"time"
)

// Serve 在停止接入后等待 bounded in-flight，返回后 caller 才关闭数据库。
func Serve(ctx context.Context, listener net.Listener, handler http.Handler, shutdown time.Duration) error {
	s := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 65 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	done := make(chan error, 1)
	go func() { done <- s.Serve(listener) }()
	select {
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		stop, cancel := context.WithTimeout(context.Background(), shutdown)
		defer cancel()
		err := s.Shutdown(stop)
		if err != nil {
			_ = s.Close()
		}
		<-done
		return err
	}
}
