import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import type { TaskFile } from "../../api/client";
import { api } from "../../api/queries";
import { DeliveryGallery, buildGalleryItems } from "./DeliveryPage";

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
  test("renders one tile per image with zoom actions and note actions only for pairs", () => {
    const items = buildGalleryItems([
      file("resources/shared/bundle_b.png", "image/png"),
      file("resources/shared/bundle_b.md", "text/markdown"),
      file("resources/shared/bundle_a.png", "image/png"),
    ]);
    const markup = renderToStaticMarkup(<DeliveryGallery taskId="task_x" items={items} />);

    expect(markup.match(/delivery-tile__/g)).toHaveLength(2 + 2 + 2); // thumb + meta + actions per tile
    expect(markup).toContain("/api/v1/tasks/task_x/files/preview?path=resources%2Fshared%2Fbundle_a.png");
    expect(markup.match(/放大<\/button>/g)).toHaveLength(2);
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
