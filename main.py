import hashlib
import logging
import os
import random
import re
import sys
import time
import requests
import fire
import redis
import urllib.parse

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

VERSION = "v1.0.5"

SENSITIVE_PATTERNS = [
    r'password\s*=\s*["\'].*?["\']',
    r'pwd\s*=\s*["\'].*?["\']',
    r'pw\s*=\s*["\'].*?["\']',
    r'secret\s*=\s*["\'].*?["\']',
    r'key\s*=\s*["\'].*?["\']',
    r'token\s*=\s*["\'].*?["\']',
    r'gsid\s*=\s*["\'].*?["\']',
    r"private key",
    r'salt\s*=\s*["\'].*?["\']',
]


class CodeReview(object):
    def __init__(self, mr: str, out_dir: str = "./output/", log_level: str = "INFO", redis_dsn: str = "redis://localhost:6379/0", prompt_version: int = 1, chat_with: str = "gpt-4o"):
        self.log_level = log_level
        self.out_dir = out_dir
        self.mr = mr
        self.redis_dsn = redis_dsn
        self.prompt_path = f"./prompts/review-diff-v{prompt_version}.prompt"
        self.chat_with = chat_with

    def setup_logging(self):
        logging.basicConfig(
            level=self.log_level,
            format="%(asctime)s - %(levelname)s - %(message)s",
            stream=sys.stdout,
        )

    def setup_apis(self):
        self.redis = redis.from_url(self.redis_dsn)
        self.review_diff_prompt = open(self.prompt_path, "r").read()

        if self.chat_with == "gpt-4o":
            self.openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=8.0, max_retries=5)
        elif self.chat_with == "coze":
            tokens = os.environ.get("COZE_TOKENS")
            bot_ids = os.environ.get("COZE_BOT_IDS")
            if not tokens or not bot_ids:
                logging.error("missing coze tokens or bot ids")
                sys.exit(1)
            self.coze_tokens = tokens.split(",")
            self.coze_bot_ids = bot_ids.split(",")

    def parse_merge_request(self):
        pattern = r"https://([^/]+)/(.+?)/([^/]+)/-/merge_requests/(\d+)(/|$)"
        x = re.match(pattern, self.mr)
        if not x:
            logging.error(f"invalid mr, url: {self.mr}")
            sys.exit(1)

        self.host = x.group(1)
        self.group = x.group(2)
        self.project = x.group(3)
        self.merge_request_id = x.group(4)

        self.init_summary()

        self.group_id = self.get_group_id()
        self.project_id = self.get_project_id()
        self.changes = self.get_changes()

    def run(self):
        self.setup_logging()
        self.setup_apis()

        self.parse_merge_request()
        self.process_diffs()

    def parse_lines(self, diff):
        old_line = 0
        new_line = 0
        for line in diff.split("\n"):
            if line.startswith("---") or line.startswith("+++"):
                continue

            if line.startswith("@@"):
                new_line = int(line.split(" ")[2].split(",")[0].replace("+", ""))
                old_line = int(line.split(" ")[1].split(",")[0].replace("-", ""))
            elif line.startswith("-"):
                return (old_line, -1)
            elif line.startswith("+"):
                return (-1, new_line)
            else:
                old_line += 1
                new_line += 1

        return (old_line, new_line)

    def init_summary(self):
        self.out_dir = os.path.join(self.out_dir, f"{self.group}-{self.project}-{self.merge_request_id}")
        self.summaries_file = f"{self.out_dir}/summaries.md"
        os.makedirs(self.out_dir, exist_ok=True)
        if os.path.exists(self.summaries_file):
            return

        with open(self.summaries_file, "w") as f:
            f.write(f"# Code Review\n\n[{self.mr}]({self.mr})\n")
            logging.info(f"create summaries, target: {self.summaries_file}")

    def write_summary(self, change_path, summary):
        with open(self.summaries_file, "a") as f:
            f.write("\n## " + change_path + "\n\n" + summary + "\n")

    def get_diff_hash(self, diff):
        return hashlib.sha1(diff.encode("utf-8")).hexdigest()

    def comment_locker_acquire(self, diff_hash):
        return self.redis.hsetnx(f"cr-comments-by-{self.chat_with}", diff_hash, 1)

    def comment_locker_release(self, diff_hash):
        self.redis.hdel(f"cr-comments-by-{self.chat_with}", diff_hash)

    def has_sensitive_code(self, diff):
        for pattern in SENSITIVE_PATTERNS:
            if re.search(pattern, diff, re.IGNORECASE):
                return True

        return False

    def process_summary(self, change, summary):
        if summary is None:
            return False

        diff_hash_id = self.get_diff_hash(change["diff"])
        self.comment_locker_acquire(diff_hash_id)

        change_path = change["old_path"]
        self.write_summary(change_path, summary)

        (old_line, new_line) = self.parse_lines(change["diff"])
        if new_line > -1:
            saved = self.post_comment(change_path, new_line, summary, "new")
        else:
            saved = self.post_comment(change_path, old_line, summary, "old")

        if saved is None:
            self.comment_locker_release(diff_hash_id)
            return False

        return True

    def process_change(self, change):
        if change["deleted_file"] or change["renamed_file"]:
            logging.warn(f"ignore, change: {change['deleted_file']}, renamed: {change['renamed_file']}, path: {change['old_path']}")
            return True

        if change["diff"] == "" or change["diff"] is None:
            logging.error(f"ignore, empty diff, path: {change['old_path']}")
            return True

        if self.has_sensitive_code(change["diff"]):
            logging.error(f"ignore, sensitive information, path: {change['old_path']}")
            self.process_summary(change, "- 严重: 存在敏感信息，请处理")
            return True

        diff_hash_id = self.get_diff_hash(change["diff"])
        if not self.comment_locker_acquire(diff_hash_id):
            logging.warn(f"already reviewed, path: {change['old_path']}")
            return True

        self.comment_locker_release(diff_hash_id)

        started_at = time.time()
        summary = self.chat(change["diff"])
        logging.info(f"review completed, with: {self.chat_with}, elapsed: {time.time() - started_at:0.2f} seconds, path: {change['old_path']}")
        return self.process_summary(change, summary)

    def process_diffs(self):
        if self.changes is None:
            logging.error(f"failed to get changes, mr: {self.mr}")
            return False

        completed = True
        for change in self.changes["changes"]:
            if not self.process_change(change):
                completed = False

        if completed:
            logging.info(f"code review was completed, mr: {self.mr}")
        else:
            logging.error(f"code review was not completed, mr: {self.mr}")

        return completed

    def post_comment(self, path, line, summaries, tag="old"):
        if self.changes is None:
            return False

        api = f"/api/v4/projects/{self.project_id}/merge_requests/{self.merge_request_id}/discussions"
        payload = {"body": summaries + "\n\n> " + "by " + self.chat_with + " " + VERSION + "，仅供参考", "position": {"base_sha": self.changes["diff_refs"]["base_sha"], "start_sha": self.changes["diff_refs"]["start_sha"], "head_sha": self.changes["diff_refs"]["head_sha"], "position_type": "text", f"{tag}_path": path, f"{tag}_line": line, "line_code": f"{path}@{line}"}}
        return self.call_git(api, "POST", payload)

    def call_git(self, api, method="GET", payload=None):
        url = f"https://{self.host}{api}"
        token = os.getenv(f"GIT_TOKEN_{self.host}")
        headers = {"PRIVATE-TOKEN": token}

        if payload is not None and method != "POST":
            url = url + "?" + urllib.parse.urlencode(payload)

        if method == "HEAD":
            response = requests.head(url, headers=headers)
            data = {}
            for k, v in response.headers.items():
                data[k] = v
            return data

        if method == "POST":
            response = requests.post(url, headers=headers, json=payload)
        else:
            response = requests.get(url, headers=headers)
        data = response.json()

        logging.debug("url: %s, response: %s, payload: %s", url, response, payload)
        if response.status_code > 299 or not data:
            logging.error("url: %s, response: %s, payload: %s, code: %d, data: %s", url, data, payload, response.status_code, data)
            return None

        return data

    def chat(self, diff):
        if self.chat_with == "gpt-4o":
            return self.chat_with_openai(diff)
        elif self.chat_with == "coze":
            return self.chat_with_coze(diff)
        else:
            logging.error(f"unknown chat_with: {self.chat_with}")
            return None

    def chat_with_coze(self, diff):
        try:
            coze_id = random.randint(0, len(self.coze_tokens) - 1)
            coze_token = self.coze_tokens[coze_id]
            coze_bot_id = self.coze_bot_ids[coze_id]
            headers = {
                "Authorization": f"Bearer {coze_token}",
                "Content-Type": "application/json",
            }
            body = {"bot_id": coze_bot_id, "user": "code-review-bot", "query": diff, "stream": False}
            response = requests.post("https://api.coze.cn/open_api/v2/chat", headers=headers, json=body)
            completion = response.json()
            if completion["code"] != 0:
                logging.error(f"coze error: {completion}, body: {body}")
                return None

            return completion["messages"][0]["content"]
        except Exception as e:
            logging.error(f"coze error: {e}")
            return None

    def chat_with_openai(self, diff):
        try:
            completion = self.openai_client.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": self.review_diff_prompt}, {"role": "user", "content": diff}])

            if completion.choices[0].message.role == "system":
                return None

            return completion.choices[0].message.content
        except Exception as e:
            logging.error(f"openai error: {e}")
            return None

    def get_id(self, api_path, query, search_field, search_value):
        data = self.call_git(api_path, payload=query)
        if data is None:
            return None
        return next((x["id"] for x in data if x[search_field] == search_value), None)

    def get_group_id(self):
        return self.get_id("/api/v4/groups", {"search": self.group}, "full_path", self.group)

    def get_project_id(self):
        return self.get_id(f"/api/v4/groups/{self.group_id}/projects", {"search": self.project}, "path", self.project)

    def get_changes(self):
        return self.call_git(f"/api/v4/projects/{self.project_id}/merge_requests/{self.merge_request_id}/changes")


if __name__ == "__main__":
    fire.Fire(CodeReview)
