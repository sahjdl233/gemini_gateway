package api

import (
	"context"
	"log"
	"time"
)

// StartSubscriptionSync 启动订阅自动更新后台任务。
// 每个 tick（30 秒）读取一次最新配置，当启用订阅自动更新且距上次同步达到设定间隔时执行一次同步。
// 配置在管理面板保存后立即失效缓存，因此修改间隔/地址无需重启即可在下一个 tick 生效。
func (adm *AdminHandler) StartSubscriptionSync() {
	go func() {
		lastSync := time.Time{}
		for {
			cfg := adm.cfg
			interval := time.Duration(cfg.SubUpdateMinutes()) * time.Minute
			if interval < time.Minute {
				interval = time.Minute
			}
			if cfg.SubUpdateEnabled() && cfg.SubURL() != "" && (lastSync.IsZero() || time.Since(lastSync) >= interval) {
				adm.syncSubscriptionOnce()
				lastSync = time.Now()
			}
			time.Sleep(30 * time.Second)
		}
	}()
}

func (adm *AdminHandler) syncSubscriptionOnce() {
	url := adm.cfg.SubURL()
	if url == "" {
		return
	}
	log.Printf("[SubSync] 开始自动更新订阅: %s", url)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	text, err := adm.fetchSubscriptionText(ctx, url)
	if err != nil {
		log.Printf("[SubSync] 拉取订阅失败: %v", err)
		return
	}
	newNodes := adm.deps.Imports.Parse(text)
	adm.markUnsupportedImports(newNodes)
	adm.deps.Exit.MergeNodes(newNodes)
	merged := make([]string, 0, len(newNodes))
	for _, cn := range newNodes {
		merged = append(merged, cn.RawURI)
	}
	adm.deps.IR.Prewarm(merged)
	log.Printf("[SubSync] 订阅更新完成，合并 %d 个节点", len(newNodes))
}

