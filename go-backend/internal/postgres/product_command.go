package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"

	"agentevalops/go-backend/internal/asset"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type ProductCommand struct {
	Conn *pgxpool.Conn
	ID   string
	lock int64
}

// BeginProductCommand 锁住同一外部命令；进程死亡时 PostgreSQL 自动释放 session 锁。
// owner 命令在自己的短事务内执行；重试使用稳定 ID 恢复已提交的 domain fact。
func BeginProductCommand(ctx context.Context, pool *pgxpool.Pool, s asset.Scope, id, route, digest string) (*ProductCommand, []byte, int, error) {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return nil, nil, 0, err
	}
	h := sha256.Sum256([]byte(id))
	c := &ProductCommand{Conn: conn, ID: id, lock: int64(binary.BigEndian.Uint64(h[:8]))}
	if _, err = conn.Exec(ctx, `SELECT pg_advisory_lock($1)`, c.lock); err != nil {
		conn.Release()
		return nil, nil, 0, err
	}
	_, err = conn.Exec(ctx, `INSERT INTO product_api_commands(id,project_id,principal_id,route,body_digest) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING`, id, s.ProjectID, s.Principal, route, digest)
	if err != nil {
		c.Close(ctx)
		return nil, nil, 0, err
	}
	var project, principal, path, body string
	var raw []byte
	var status *int
	err = conn.QueryRow(ctx, `SELECT project_id::text,principal_id,route,body_digest,response,status FROM product_api_commands WHERE id=$1`, id).Scan(&project, &principal, &path, &body, &raw, &status)
	if err != nil || project != s.ProjectID || principal != s.Principal || path != route || body != digest {
		c.Close(ctx)
		if err != nil {
			return nil, nil, 0, err
		}
		return nil, nil, 0, asset.ErrConflict
	}
	if status != nil {
		return c, raw, *status, nil
	}
	return c, nil, 0, nil
}
func (c *ProductCommand) Complete(ctx context.Context, raw []byte, status int) error {
	_, err := c.Conn.Exec(ctx, `UPDATE product_api_commands SET response=$2,status=$3 WHERE id=$1 AND response IS NULL`, c.ID, raw, status)
	return err
}
func (c *ProductCommand) Close(ctx context.Context) {
	// 不把仍持有 session lock 的连接交回 pool。
	var unlocked bool
	err := c.Conn.QueryRow(ctx, `SELECT pg_advisory_unlock($1)`, c.lock).Scan(&unlocked)
	if err != nil || !unlocked {
		conn := c.Conn.Hijack()
		_ = conn.Close(context.Background())
		return
	}
	c.Conn.Release()
}
func (k ProductReader) ResultRun(ctx context.Context, s asset.Scope, id string) (string, error) {
	var run string
	err := k.Pool.QueryRow(ctx, `SELECT r.run_id::text FROM evaluation_results r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&run)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", asset.ErrNotFound
	}
	return run, err
}
