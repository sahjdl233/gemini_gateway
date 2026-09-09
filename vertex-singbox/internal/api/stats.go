package api

import (
	"sort"
	"sync"
	"time"
)

// modelStat 记录单个模型的调用统计（进程内内存态）。
// 仅统计真实模型回复（generateContent / streamGenerateContent / countTokens），
// 不统计 /model 元数据查询。
type modelStat struct {
	Success int64   `json:"success"`
	Fail    int64   `json:"fail"`
	TotalMs float64 `json:"total_ms"`
}

type statsCollector struct {
	mu      sync.Mutex
	byModel map[string]*modelStat
	started time.Time
	recent  []statEntry
}

type statEntry struct {
	Time     int64   `json:"time"`
	Model    string  `json:"model"`
	OK       bool    `json:"ok"`
	Elapsed  float64 `json:"elapsed_ms"`
	Endpoint string  `json:"endpoint"`
}

const (
	recentCap    = 200
	recentWindow = 24 * time.Hour
)

var collector = &statsCollector{
	byModel: make(map[string]*modelStat),
	started: time.Now(),
}

func recordStat(model string, ok bool, elapsed time.Duration, endpoint string) {
	if model == "" {
		return
	}
	collector.mu.Lock()
	defer collector.mu.Unlock()

	st := collector.byModel[model]
	if st == nil {
		st = &modelStat{}
		collector.byModel[model] = st
	}
	if ok {
		st.Success++
	} else {
		st.Fail++
	}
	st.TotalMs += float64(elapsed.Milliseconds())

	now := time.Now()
	collector.recent = append(collector.recent, statEntry{
		Time:     now.UnixMilli(),
		Model:    model,
		OK:       ok,
		Elapsed:  float64(elapsed.Milliseconds()),
		Endpoint: endpoint,
	})
	cutoff := now.Add(-recentWindow).UnixMilli()
	keep := collector.recent[:0]
	for _, e := range collector.recent {
		if e.Time >= cutoff {
			keep = append(keep, e)
		}
	}
	collector.recent = keep
	if len(collector.recent) > recentCap {
		collector.recent = collector.recent[len(collector.recent)-recentCap:]
	}
}

func resetStats() {
	collector.mu.Lock()
	defer collector.mu.Unlock()
	collector.byModel = make(map[string]*modelStat)
	collector.recent = collector.recent[:0]
	collector.started = time.Now()
}

func statsSnapshot() map[string]any {
	collector.mu.Lock()
	defer collector.mu.Unlock()

	models := make([]map[string]any, 0, len(collector.byModel))
	var totalSuccess, totalFail int64
	var totalMs float64
	for name, st := range collector.byModel {
		totalSuccess += st.Success
		totalFail += st.Fail
		totalMs += st.TotalMs
		models = append(models, map[string]any{
			"model":        name,
			"success":      st.Success,
			"fail":         st.Fail,
			"total":        st.Success + st.Fail,
			"success_rate": rateOf(st.Success, st.Success+st.Fail),
			"avg_ms":       avgOf(st.TotalMs, st.Success+st.Fail),
			"total_ms":     st.TotalMs,
		})
	}
	sort.Slice(models, func(i, j int) bool {
		return models[i]["total"].(int64) > models[j]["total"].(int64)
	})

	recent := make([]map[string]any, 0, len(collector.recent))
	for _, e := range collector.recent {
		recent = append(recent, map[string]any{
			"time":       e.Time,
			"model":      e.Model,
			"ok":         e.OK,
			"elapsed_ms": e.Elapsed,
			"endpoint":   e.Endpoint,
		})
	}
	for i, j := 0, len(recent)-1; i < j; i, j = i+1, j-1 {
		recent[i], recent[j] = recent[j], recent[i]
	}

	total := totalSuccess + totalFail
	return map[string]any{
		"started":       collector.started.Unix(),
		"total_success": totalSuccess,
		"total_fail":    totalFail,
		"total":         total,
		"success_rate":  rateOf(totalSuccess, total),
		"avg_ms":        avgOf(totalMs, total),
		"models":        models,
		"recent":        recent,
	}
}

func rateOf(success, total int64) float64 {
	if total == 0 {
		return 0
	}
	return float64(success) / float64(total) * 100
}

func avgOf(totalMs float64, count int64) float64 {
	if count == 0 {
		return 0
	}
	return totalMs / float64(count)
}

