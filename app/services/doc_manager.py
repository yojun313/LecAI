# app/services/doc_manager.py
import os
import shutil
import uuid
import zipfile
from datetime import datetime
from app.core.config import settings
from app.db import docs_col


class DocManager:
    @staticmethod
    def get_zip_path(owner: str, doc_id: str):
        target = docs_col.find_one({"id": doc_id, "owner": owner})

        if not target or target["type"] != "file":
            return None

        source_dir = os.path.join(settings.DOCS_STATIC_DIR, owner, doc_id)
        zip_output_base = os.path.join(
            settings.UPLOAD_DIR, f"download_{owner}_{doc_id}"
        )

        if not os.path.exists(source_dir):
            return None

        zip_path = shutil.make_archive(zip_output_base, "zip", source_dir)
        return zip_path

    @staticmethod
    def rename_node(owner: str, node_id: str, new_name: str):
        if not new_name or not new_name.strip():
            return False

        node = docs_col.find_one({"id": node_id, "owner": owner})
        if not node:
            return False

        docs_col.update_one({"id": node_id}, {"$set": {"name": new_name.strip()}})
        return True

    @staticmethod
    def move_node(owner: str, node_id: str, target_parent_id: str = None):
        node = docs_col.find_one({"id": node_id, "owner": owner})
        if not node:
            return False

        if node_id == target_parent_id:
            return False

        if node.get("parent_id") == target_parent_id:
            return True

        if target_parent_id:
            target_folder = docs_col.find_one({"id": target_parent_id, "owner": owner})
            if not target_folder or target_folder["type"] != "folder":
                return False

            if node["type"] == "folder":
                current_id = target_parent_id
                while current_id:
                    if current_id == node_id:
                        return False
                    parent = docs_col.find_one({"id": current_id}, {"parent_id": 1})
                    if not parent:
                        break
                    current_id = parent.get("parent_id")

        docs_col.update_one({"id": node_id}, {"$set": {"parent_id": target_parent_id}})
        return True

    @staticmethod
    def get_nodes(owner: str, parent_id: str = None):
        query = {"owner": owner, "parent_id": parent_id}
        nodes = list(docs_col.find(query, {"_id": 0}))
        return sorted(nodes, key=lambda x: (x["type"] != "folder", x["name"]))

    @staticmethod
    def create_folder(owner: str, name: str, parent_id: str = None):
        new_folder = {
            "id": str(uuid.uuid4()),
            "type": "folder",
            "name": name,
            "owner": owner,
            "parent_id": parent_id,
            "created_at": datetime.now().isoformat(),
        }
        docs_col.insert_one(new_folder)
        new_folder.pop("_id", None)
        return new_folder

    @staticmethod
    def upload_zip_doc(
        owner: str, file_path: str, filename: str, parent_id: str = None
    ):
        doc_id = str(uuid.uuid4())

        extract_path = os.path.join(settings.DOCS_STATIC_DIR, owner, doc_id)
        os.makedirs(extract_path, exist_ok=True)

        try:
            with zipfile.ZipFile(file_path, "r") as zip_ref:
                zip_ref.extractall(extract_path)
        except Exception as e:
            if os.path.exists(extract_path):
                shutil.rmtree(extract_path)
            raise e

        if not os.path.exists(os.path.join(extract_path, "result.md")):
            items = os.listdir(extract_path)
            visible_items = [
                i for i in items if not i.startswith(".") and not i.startswith("__")
            ]

            if len(visible_items) == 1:
                nested_dir = os.path.join(extract_path, visible_items[0])
                if os.path.isdir(nested_dir):
                    for item in os.listdir(nested_dir):
                        src_path = os.path.join(nested_dir, item)
                        dst_path = os.path.join(extract_path, item)
                        if os.path.exists(dst_path):
                            if os.path.isdir(dst_path):
                                shutil.rmtree(dst_path)
                            else:
                                os.remove(dst_path)
                        shutil.move(src_path, extract_path)
                    os.rmdir(nested_dir)

        doc_name = os.path.splitext(filename)[0]

        new_doc = {
            "id": doc_id,
            "type": "file",
            "name": doc_name,
            "owner": owner,
            "parent_id": parent_id,
            "path": f"/static/docs/{owner}/{doc_id}",
            "created_at": datetime.now().isoformat(),
        }

        docs_col.insert_one(new_doc)
        new_doc.pop("_id", None)
        return new_doc

    @staticmethod
    def delete_node(owner: str, node_id: str):
        target = docs_col.find_one({"id": node_id, "owner": owner})
        if not target:
            return False

        children = docs_col.find({"parent_id": node_id})
        for child in children:
            DocManager.delete_node(owner, child["id"])

        if target["type"] == "file":
            full_path = os.path.join(settings.DOCS_STATIC_DIR, owner, target["id"])
            if os.path.exists(full_path):
                shutil.rmtree(full_path)

        docs_col.delete_one({"id": node_id})
        return True

    @staticmethod
    def get_markdown_content(owner: str, doc_id: str):
        target = docs_col.find_one({"id": doc_id, "owner": owner})
        if not target:
            return None

        md_path = os.path.join(settings.DOCS_STATIC_DIR, owner, doc_id, "result.md")

        if not os.path.exists(md_path):
            return "# Error: Markdown file not found."

        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        content = content.replace("./images/", f"{target['path']}/images/")
        return content
