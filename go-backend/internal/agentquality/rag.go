// Package agentquality 拥有质量判断；不改变 execution truth。
package agentquality

import (
	"math"
	"sort"

	"agentevalops/go-backend/internal/asset"
)

type Chunk struct {
	Document string `json:"document_id"`
	Chunk    string `json:"chunk_id"`
}
type Graded struct {
	Document  *string `json:"document_id"`
	Chunk     string  `json:"chunk_id"`
	Relevance int     `json:"relevance"`
}
type GroundTruth struct {
	Relevant []Chunk  `json:"relevant_chunks"`
	Graded   []Graded `json:"graded_relevance"`
}
type RankedItem struct {
	Document        string     `json:"document_id"`
	Chunk           string     `json:"chunk_id"`
	Rank            int        `json:"rank"`
	RetrievalRank   int        `json:"retrieval_rank"`
	RerankRank      *int       `json:"rerank_rank"`
	RetrievalScore  float64    `json:"retrieval_score"`
	ScoreKind       string     `json:"retrieval_score_kind"`
	Channels        []string   `json:"retrieval_channels"`
	RerankScore     *float64   `json:"rerank_score"`
	RerankScoreKind *string    `json:"rerank_score_kind"`
	Source          asset.JSON `json:"source"`
	Page            *int       `json:"page"`
	Section         *string    `json:"section"`
	Sheet           *string    `json:"sheet"`
	ContentHash     *string    `json:"content_hash"`
	Selected        *bool      `json:"selected"`
	DenseRank       *int       `json:"dense_channel_rank"`
	BM25Rank        *int       `json:"bm25_channel_rank"`
	RRFRank         *int       `json:"rrf_fused_rank"`
}
type SelectedItem struct {
	Document string `json:"document_id"`
	Chunk    string `json:"chunk_id"`
	Rank     int    `json:"selection_rank"`
	Block    string `json:"context_block_id"`
	Citation string `json:"citation_id"`
	Hash     string `json:"context_content_hash"`
	Text     string `json:"text"`
}
type Citation struct {
	ID       string  `json:"citation_id"`
	Document string  `json:"document_id"`
	Chunk    string  `json:"chunk_id"`
	Block    string  `json:"context_block_id"`
	Hash     string  `json:"context_content_hash"`
	Label    string  `json:"display_label"`
	Page     *int    `json:"page"`
	Section  *string `json:"section"`
}
type RAGArtifact struct {
	Schema           string         `json:"schema_version"`
	ID               string         `json:"artifact_id"`
	RunID            string         `json:"run_id"`
	AttemptID        string         `json:"attempt_id"`
	RetrievalID      string         `json:"retrieval_id"`
	Invocation       int            `json:"invocation_index"`
	Status           string         `json:"retrieval_status"`
	Query            string         `json:"query"`
	Rewritten        string         `json:"rewritten_query"`
	Retrieved        []RankedItem   `json:"retrieved_items"`
	Ranked           []RankedItem   `json:"ranked_items"`
	Selected         []SelectedItem `json:"selected_items"`
	Citations        []Citation     `json:"citations"`
	RetrievalLatency *int           `json:"retrieval_latency_ms"`
	RerankLatency    *int           `json:"rerank_latency_ms"`
	TotalLatency     *int           `json:"total_latency_ms"`
	Degraded         *bool          `json:"degraded"`
	Degradation      []string       `json:"degradation_reasons"`
	Error            asset.JSON     `json:"error"`
	Budget           asset.JSON     `json:"budget_usage"`
	Strategy         *string        `json:"retrieval_strategy"`
	Provenance       *string        `json:"provenance_sha256"`
	Generation       *string        `json:"generation_id"`
	Identity         *string        `json:"identity_sha256"`
	RewriteFixture   *string        `json:"rewrite_fixture_id"`
	QueryDigest      *string        `json:"query_digest"`
	RewrittenDigest  *string        `json:"rewritten_query_digest"`
}

func oneOf(v string, values ...string) bool {
	for _, x := range values {
		if v == x {
			return true
		}
	}
	return false
}

// DecodeRAG 校验 required 字段的存在性，区分零值与字段缺失。
func DecodeRAG(j asset.JSON, remote string) (RAGArtifact, error) {
	var r RAGArtifact
	var fields map[string]asset.JSON
	if j.Decode(&r) != nil || j.Decode(&fields) != nil {
		return r, asset.ErrInvalid
	}
	for _, key := range []string{"schema_version", "artifact_id", "run_id", "attempt_id", "retrieval_id", "invocation_index", "retrieval_status", "query", "rewritten_query", "retrieved_items", "ranked_items", "selected_items", "citations", "total_latency_ms", "degraded", "degradation_reasons", "budget_usage"} {
		if v, ok := fields[key]; !ok || v.String() == "null" {
			return r, asset.ErrInvalid
		}
	}
	for key, required := range map[string][]string{"retrieved_items": {"document_id", "chunk_id", "rank", "retrieval_rank", "retrieval_score", "retrieval_score_kind", "retrieval_channels", "source", "selected"}, "ranked_items": {"document_id", "chunk_id", "rank", "retrieval_rank", "retrieval_score", "retrieval_score_kind", "retrieval_channels", "source", "selected"}, "selected_items": {"document_id", "chunk_id", "selection_rank", "context_block_id", "citation_id", "context_content_hash", "text"}, "citations": {"citation_id", "document_id", "chunk_id", "context_block_id", "context_content_hash", "display_label"}} {
		var items []map[string]asset.JSON
		if fields[key].Decode(&items) != nil {
			return r, asset.ErrInvalid
		}
		for _, item := range items {
			for _, f := range required {
				if v, ok := item[f]; !ok || v.String() == "null" {
					return r, asset.ErrInvalid
				}
			}
		}
	}
	return r, r.Validate(remote)
}
func (r RAGArtifact) Validate(remote string) error {
	if !oneOf(r.Schema, "rag-evaluation-artifact.v1", "rag-evaluation-artifact.v2") || r.RunID != remote || r.AttemptID != remote || !asset.Text(r.RetrievalID) || r.ID != "rag-eval://"+remote+"/"+r.RetrievalID || r.Invocation < 1 || !oneOf(r.Status, "SUCCEEDED", "EMPTY", "DEGRADED", "FAILED", "TIMED_OUT", "CANCELLED") || r.Retrieved == nil || r.Ranked == nil || r.Selected == nil || r.Citations == nil || r.TotalLatency == nil || r.Degraded == nil || r.Degradation == nil {
		return asset.ErrInvalid
	}
	var budget struct {
		Retrieval int `json:"retrieval_calls"`
		Embedding int `json:"embedding_calls"`
		Vector    int `json:"vector_queries"`
		Keyword   int `json:"keyword_queries"`
		BM25      int `json:"bm25_queries"`
		RRF       int `json:"rrf_fusions"`
		Reads     int `json:"document_reads"`
		Chars     int `json:"context_chars"`
	}
	if r.Budget.String() == "null" || r.Budget.Decode(&budget) != nil {
		return asset.ErrInvalid
	}
	if r.Error.String() != "null" {
		var e struct {
			Category string  `json:"category"`
			Code     string  `json:"safe_error_code"`
			Message  string  `json:"safe_message"`
			Stage    *string `json:"stage"`
			Failed   int     `json:"failed_source_count"`
		}
		if r.Error.Decode(&e) != nil || e.Category == "" || e.Code == "" {
			return asset.ErrInvalid
		}
	}
	validate := func(i RankedItem) bool {
		var source struct {
			Type       *string `json:"source_type"`
			Collection *string `json:"collection"`
			Display    *string `json:"display_name"`
			Version    *string `json:"document_version"`
		}
		if i.Document == "" || i.Chunk == "" || i.Rank < 1 || i.RetrievalRank < 1 || i.Selected == nil || i.Channels == nil || i.Source.Decode(&source) != nil || source.Type == nil || source.Collection == nil || source.Display == nil || source.Version == nil {
			return false
		}
		kinds := []string{"VECTOR", "KEYWORD", "VECTOR_NORMALIZED_RELEVANCE", "KEYWORD_FIXED_HEURISTIC", "HEURISTIC_RERANK", "BM25_RAW_SCORE", "RRF_SCORE"}
		if !oneOf(i.ScoreKind, kinds...) || (i.RerankScoreKind != nil && !oneOf(*i.RerankScoreKind, kinds...)) {
			return false
		}
		if r.Schema == "rag-evaluation-artifact.v2" && (oneOf(i.ScoreKind, "VECTOR", "KEYWORD") || (i.RerankScoreKind != nil && oneOf(*i.RerankScoreKind, "VECTOR", "KEYWORD"))) {
			return false
		}
		for _, c := range i.Channels {
			if !oneOf(c, "VECTOR", "KEYWORD", "VECTOR_REWRITTEN_QUERY", "VECTOR_ORIGINAL_QUERY", "VECTOR_ORIGINAL_AND_REWRITTEN", "BM25", "RRF") || (r.Schema == "rag-evaluation-artifact.v2" && oneOf(c, "VECTOR", "KEYWORD")) {
				return false
			}
		}
		for _, p := range []*int{i.RerankRank, i.DenseRank, i.BM25Rank, i.RRFRank} {
			if p != nil && *p < 1 {
				return false
			}
		}
		return true
	}
	retrieved, ranked := map[Chunk]bool{}, map[Chunk]bool{}
	for _, i := range r.Retrieved {
		if !validate(i) {
			return asset.ErrInvalid
		}
		retrieved[Chunk{i.Document, i.Chunk}] = true
	}
	for _, i := range r.Ranked {
		key := Chunk{i.Document, i.Chunk}
		if !validate(i) || !retrieved[key] {
			return asset.ErrInvalid
		}
		ranked[key] = true
	}
	for _, i := range r.Selected {
		if !ranked[Chunk{i.Document, i.Chunk}] || i.Rank < 1 || i.Block == "" || i.Citation == "" || i.Hash == "" {
			return asset.ErrInvalid
		}
	}
	return nil
}

type RankingValue struct {
	Score  *float64                 `json:"score"`
	State  asset.ApplicabilityState `json:"applicability"`
	Source string                   `json:"rank_source"`
}

func rankingState(s asset.ApplicabilityState) RankingValue { return RankingValue{State: s} }
func rankedValue(n float64, s string) RankingValue {
	return RankingValue{Score: &n, State: asset.Applicable, Source: s}
}

// Ranking 使用 artifact 原始 rank；重复 identity 占据窗口，但只贡献一次相关性。
func Ranking(metric string, k int, gt *GroundTruth, r RAGArtifact) (RankingValue, error) {
	if k < 1 {
		return RankingValue{}, asset.ErrInvalid
	}
	if gt == nil {
		return rankingState(asset.MissingEvidence), nil
	}
	if metric == "recall@k.v1" || metric == "mrr.v1" {
		if gt.Relevant == nil {
			return rankingState(asset.MissingEvidence), nil
		}
		if len(gt.Relevant) == 0 {
			return rankingState(asset.NotApplicable), nil
		}
		relevant := map[Chunk]bool{}
		for _, c := range gt.Relevant {
			if c.Document == "" || c.Chunk == "" || relevant[c] {
				return RankingValue{}, asset.ErrInvalid
			}
			relevant[c] = true
		}
		items := append([]RankedItem(nil), r.Retrieved...)
		source := "retrieved_items"
		rank := func(i RankedItem) int { return i.RetrievalRank }
		if metric == "mrr.v1" || (r.Strategy != nil && *r.Strategy == "HYBRID_RRF") {
			if len(r.Ranked) > 0 || (r.Strategy != nil && *r.Strategy == "HYBRID_RRF") {
				items = append([]RankedItem(nil), r.Ranked...)
				rank = func(i RankedItem) int { return i.Rank }
				source = "ranked_items"
			}
		}
		if metric == "mrr.v1" {
			first := 0
			for _, i := range items {
				n := rank(i)
				if relevant[Chunk{i.Document, i.Chunk}] && (first == 0 || n < first) {
					first = n
				}
			}
			if first == 0 {
				return rankedValue(0, source), nil
			}
			if first < 1 {
				return RankingValue{}, asset.ErrInvalid
			}
			return rankedValue(1/float64(first), source), nil
		}
		sort.SliceStable(items, func(i, j int) bool { return rank(items[i]) < rank(items[j]) })
		hits := map[Chunk]bool{}
		for _, i := range items[:min(k, len(items))] {
			key := Chunk{i.Document, i.Chunk}
			if relevant[key] {
				hits[key] = true
			}
		}
		return rankedValue(float64(len(hits))/float64(len(relevant)), source), nil
	}
	if metric != "ndcg.v1" {
		return RankingValue{}, asset.ErrUnsupported
	}
	if gt.Graded == nil {
		return rankingState(asset.MissingEvidence), nil
	}
	if len(gt.Graded) == 0 {
		return rankingState(asset.NotApplicable), nil
	}
	ranks := map[Chunk]int{}
	docs := map[string]map[string]bool{}
	for _, i := range r.Ranked {
		if i.Rank < 1 {
			return RankingValue{}, asset.ErrInvalid
		}
		c := Chunk{i.Document, i.Chunk}
		if ranks[c] == 0 || i.Rank < ranks[c] {
			ranks[c] = i.Rank
		}
		if docs[i.Chunk] == nil {
			docs[i.Chunk] = map[string]bool{}
		}
		docs[i.Chunk][i.Document] = true
	}
	used := map[Chunk]bool{}
	seenGT := map[string]bool{}
	ideal := []int{}
	dcg := 0.0
	for _, g := range gt.Graded {
		if g.Relevance < 0 || g.Relevance > 1023 || g.Chunk == "" {
			return RankingValue{}, asset.ErrInvalid
		}
		j, _ := asset.Freeze(struct {
			Document *string
			Chunk    string
		}{g.Document, g.Chunk})
		if seenGT[j.String()] {
			return RankingValue{}, asset.ErrInvalid
		}
		seenGT[j.String()] = true
		ideal = append(ideal, g.Relevance)
		doc := ""
		if g.Document != nil {
			doc = *g.Document
		} else {
			if len(docs[g.Chunk]) > 1 {
				return RankingValue{}, asset.ErrInvalid
			}
			for d := range docs[g.Chunk] {
				doc = d
			}
		}
		c := Chunk{doc, g.Chunk}
		n := ranks[c]
		if n == 0 {
			continue
		}
		if used[c] {
			return RankingValue{}, asset.ErrInvalid
		}
		used[c] = true
		if n <= k {
			dcg += (math.Exp2(float64(g.Relevance)) - 1) / math.Log2(float64(n+1))
		}
	}
	sort.Sort(sort.Reverse(sort.IntSlice(ideal)))
	idcg := 0.0
	for i, g := range ideal[:min(k, len(ideal))] {
		idcg += (math.Exp2(float64(g)) - 1) / math.Log2(float64(i+2))
	}
	if idcg == 0 {
		return rankedValue(0, "ranked_items"), nil
	}
	n := dcg / idcg
	if math.IsNaN(n) || math.IsInf(n, 0) || n > 1 {
		return RankingValue{}, asset.ErrInvalid
	}
	return rankedValue(n, "ranked_items"), nil
}
