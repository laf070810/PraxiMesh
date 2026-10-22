"use strict";

const editor = document.querySelector("#editor-form");
if (editor) {
  const status = document.querySelector("#editor-status");
  const source = document.querySelector("#source");
  let dirty = false;
  source.addEventListener("input", () => {
    dirty = true;
    status.textContent = gettext("尚未保存");
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  editor.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    if (!button) return;
    const draft = button.dataset.action === "draft";
    const data = new FormData(editor);
    const sentSource = source.value;
    if (!draft) data.set("mode", button.value);
    const buttons = [...editor.querySelectorAll("button")];
    const previous = buttons.map((b) => b.disabled);
    buttons.forEach((b) => {
      b.disabled = true;
    });
    status.textContent = draft ? gettext("正在保存…") : gettext("正在提交…");
    try {
      const response = await fetch(
        draft ? editor.dataset.draftUrl : editor.action,
        {
          method: "POST",
          body: data,
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        },
      );
      if (!response.headers.get("content-type")?.includes("application/json")) {
        throw new Error(gettext("登录可能已过期。请先保留代码，再刷新并登录。"));
      }
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || gettext("操作失败，请稍后重试。"));
      dirty = source.value !== sentSource;
      if (draft)
        status.textContent = dirty
          ? gettext("上一份草稿已保存；当前还有修改。")
          : gettext("草稿已私密保存");
      else {
        if (dirty) {
          status.textContent =
            gettext("已提交所选代码；编辑区的新修改尚未保存。请到我的学习查看结果。");
        } else window.location.assign(body.url);
      }
    } catch (error) {
      status.textContent = error.message || gettext("连接中断，请稍后重试。");
    } finally {
      buttons.forEach((b, i) => {
        b.disabled = previous[i];
      });
    }
  });
}

const resultPanel = document.querySelector("[data-poll-url]");
if (resultPanel) {
  let failures = 0;
  const poll = async () => {
    try {
      const response = await fetch(resultPanel.dataset.pollUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok || response.redirected)
        throw new Error(gettext("暂时无法读取状态，请刷新页面或重新登录。"));
      const data = await response.json();
      if (data.status === "finished") {
        window.location.reload();
        return;
      }
      document.querySelector("#poll-status").textContent = data.status_label;
      failures = 0;
    } catch (error) {
      document.querySelector("#poll-status").textContent =
        gettext("连接暂时中断，正在重试。你也可以稍后在提交历史查看结果。");
      failures += 1;
    }
    if (failures < 10)
      window.setTimeout(poll, Math.min(15000, 1500 * (failures + 1)));
  };
  window.setTimeout(poll, 1500);
}
