import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import type { TaskFile } from "../../api/client";
import { api } from "../../api/queries";
import {
  DeliveryFileList,
  DeliveryGallery,
  DeliveryViewTabs,
  buildFileListItems,
  buildGalleryItems,
} from "./DeliveryPage";

function file(relativePath: string, mimeType: string, size = 128): TaskFile {
  return {
    relative_path: relativePath,
    filename: relativePath.split("/").pop() ?? relativePath,
    mime_type: mimeType,
    size_bytes: size,
    sha256: "0".repeat(64),
    previewable: true,
  };
}

describe("buildGalleryItems", () => {
  test("keeps only shared images and pairs each with its same-basename md", () => {
    const items = buildGalleryItems([
      file("resources/shared/bundle_b.png", "image/png"),
      file("resources/shared/bundle_b.md", "text/markdown"),
      file("resources/shared/bundle_a.png", "image/png"),
      file("resources/shared/notes/agent-log.txt", "text/plain"),
      file("resources/manifests/a_pub_1.json", "application/json"),
      file("inputs/original/brief.md", "text/markdown"),
      file("instances/i_image_1/outputs/draft.png", "image/png"),
    ]);

    expect(items.map((item) => item.file.filename)).toEqual(["bundle_a.png", "bundle_b.png"]);
    expect(items[0]?.notePath).toBeNull();
    expect(items[1]?.notePath).toBe("resources/shared/bundle_b.md");
  });
});

describe("DeliveryGallery", () => {
  test("renders one clean tile per image with note action only for pairs", () => {
    const items = buildGalleryItems([
      file("resources/shared/bundle_b.png", "image/png"),
      file("resources/shared/bundle_b.md", "text/markdown"),
      file("resources/shared/bundle_a.png", "image/png"),
    ]);
    const markup = renderToStaticMarkup(<DeliveryGallery taskId="task_x" items={items} />);

    expect(markup.match(/delivery-tile__thumb/g)).toHaveLength(2);
    expect(markup).toContain("/api/v1/tasks/task_x/files/preview?path=resources%2Fshared%2Fbundle_a.png");
    expect(markup).not.toContain("delivery-tile__meta");
    expect(markup).not.toContain(">放大</button>");
    expect(markup.match(/设计理念<\/button>/g)).toHaveLength(1);
    expect(markup.match(/aria-label="放大查看 /g)).toHaveLength(2);
  });
});

describe("shared archive download", () => {
  test("points at the shared zip endpoint", () => {
    expect(api.sharedArchiveUrl("task_x")).toBe(
      "/api/v1/tasks/task_x/files/download-archive?group=shared",
    );
  });
});

describe("buildFileListItems", () => {
  test("keeps every user-visible shared file, sorted, without internal entries", () => {
    const items = buildFileListItems([
      file("resources/shared/z_photo.png", "image/png"),
      file("resources/shared/a_intro.md", "text/markdown"),
      file("resources/shared/notes/agent-log.txt", "text/plain"),
      file("resources/shared/.general-agent-state/state_secret.json", "application/json"),
      file("resources/shared/.pub_inflight.tmp", "application/octet-stream"),
      file("resources/manifests/a_pub_1.json", "application/json"),
      file("inputs/original/brief.md", "text/markdown"),
      file("instances/i_image_1/outputs/draft.png", "image/png"),
    ]);

    expect(items.map((item) => item.relative_path)).toEqual([
      "resources/shared/a_intro.md",
      "resources/shared/notes/agent-log.txt",
      "resources/shared/z_photo.png",
    ]);
  });
});

describe("DeliveryFileList", () => {
  test("renders one row per file with a download link and no preview action", () => {
    const items = buildFileListItems([
      file("resources/shared/学院介绍.md", "text/markdown", 2048),
      file("resources/shared/bundle_a.png", "image/png", 128),
    ]);
    const markup = renderToStaticMarkup(<DeliveryFileList taskId="task_x" items={items} />);

    expect(markup.match(/delivery-file-item"/g)).toHaveLength(2);
    expect(markup).toContain("学院介绍.md");
    expect(markup).toContain("2.0 KB");
    expect(markup).toContain(
      "/api/v1/tasks/task_x/files/download?path=resources%2Fshared%2F%E5%AD%A6%E9%99%A2%E4%BB%8B%E7%BB%8D.md",
    );
    expect(markup.match(/aria-label="下载 /g)).toHaveLength(2);
    expect(markup.match(/下载<\/a>/g)).toHaveLength(2);
    expect(markup).not.toContain("files/preview");
  });
});

describe("DeliveryViewTabs", () => {
  test("marks the active view and exposes both tabs", () => {
    const markup = renderToStaticMarkup(<DeliveryViewTabs view="files" onChange={() => undefined} />);

    expect(markup).toContain('role="tablist"');
    expect(markup.match(/role="tab"/g)).toHaveLength(2);
    expect(markup).toContain('id="delivery-view-tab-files" type="button" role="tab" aria-selected="true"');
    expect(markup).toContain('id="delivery-view-tab-gallery" type="button" role="tab" aria-selected="false"');
  });
});
