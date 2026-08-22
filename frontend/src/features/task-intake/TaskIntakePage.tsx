export function TaskIntakePage(): React.JSX.Element {
  return (
    <section className="workbench-page" aria-labelledby="task-intake-title">
      <header className="workbench-page__header">
        <div>
          <p className="workbench-eyebrow">新任务</p>
          <h1 id="task-intake-title">创建新的设计任务</h1>
          <p>Master 将先分析目标与材料，再生成计划供确认。</p>
        </div>
        <span className="workbench-phase-badge"><span aria-hidden="true" />F0 框架就绪</span>
      </header>

      <div className="workbench-intake-card">
        <label className="workbench-field" htmlFor="task-prompt-foundation">
          <span>Prompt</span>
          <textarea
            id="task-prompt-foundation"
            disabled
            rows={6}
            placeholder="F1 将在此接入可恢复 DRAFT 与就地校验。"
          />
          <small>输入、上传与提交写链路将在 F1 接入；F0 不创建浏览器侧伪任务。</small>
        </label>
        <div className="workbench-upload-foundation" aria-labelledby="upload-foundation-title">
          <div>
            <p className="workbench-eyebrow">首次材料</p>
            <h2 id="upload-foundation-title">图片、PDF、TXT / MD</h2>
            <p>上传队列将逐文件展示进度、说明、重试与取消。</p>
          </div>
          <button type="button" disabled>等待 F1 接入</button>
        </div>
      </div>

      <div className="workbench-foundation-grid" aria-label="F0 基线">
        <article><strong>契约事实源</strong><p>TaskIntake、MasterMessage、PlanProposal 与 WorkItem 已由生成类型约束。</p></article>
        <article><strong>路由可恢复</strong><p>根路径进入新任务页，任务上下文与详情抽屉可直接深链。</p></article>
        <article><strong>旧功能可用</strong><p>任务面板、资源、审批、用量、实例与设置继续由已验收路由承载。</p></article>
      </div>
    </section>
  );
}
