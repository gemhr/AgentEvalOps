FROM golang:1.26-bookworm AS builder
WORKDIR /src
COPY go-backend/go.mod go-backend/go.sum ./
RUN go mod download
COPY go-backend/ ./
ARG GIT_SHA=unknown
ARG GIT_DIRTY=unknown
ARG BUILD_TIME=unknown
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X agentevalops/go-backend/internal/buildinfo.Revision=${GIT_SHA} -X agentevalops/go-backend/internal/buildinfo.Dirty=${GIT_DIRTY} -X agentevalops/go-backend/internal/buildinfo.BuiltAt=${BUILD_TIME}" -o /out/api ./cmd/api && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X agentevalops/go-backend/internal/buildinfo.Revision=${GIT_SHA} -X agentevalops/go-backend/internal/buildinfo.Dirty=${GIT_DIRTY} -X agentevalops/go-backend/internal/buildinfo.BuiltAt=${BUILD_TIME}" -o /out/worker ./cmd/worker && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X agentevalops/go-backend/internal/buildinfo.Revision=${GIT_SHA} -X agentevalops/go-backend/internal/buildinfo.Dirty=${GIT_DIRTY} -X agentevalops/go-backend/internal/buildinfo.BuiltAt=${BUILD_TIME}" -o /out/evalgate ./cmd/evalgate

FROM scratch AS runtime
WORKDIR /app
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=builder /usr/share/zoneinfo /usr/share/zoneinfo
COPY --from=builder /out/ /app/
USER 10001:10001
EXPOSE 8081
ENTRYPOINT ["/app/api"]
CMD ["--listen", "0.0.0.0:8081"]

FROM runtime AS worker
ENTRYPOINT ["/app/worker"]
CMD []

FROM runtime AS api
