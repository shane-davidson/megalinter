#!/usr/bin/env python3
"""
API Reporter
Send MegaLinter results to an external API, like Grafana Loki
"""
import copy
import json
import logging
import os
import time

import git
import requests
from megalinter import Reporter, config
from megalinter.constants import ML_DOC_URL_DESCRIPTORS_ROOT


class ApiReporter(Reporter):
    name = "API_REPORTER"

    api_url: str | None = None
    payload: object = {}
    linter_payloads = []
    payloadFormatted: object = {}

    def __init__(self, params=None):
        # Deactivate Api reporter by default
        self.is_active = False
        self.processing_order = 20  # Run after text reporter
        super().__init__(params)

    def manage_activation(self):
        if config.get(self.master.request_id, "API_REPORTER", "false") == "true":
            if config.exists(self.master.request_id, "API_REPORTER_URL"):
                self.is_active = True
                self.api_url = config.get(self.master.request_id, "API_REPORTER_URL")
            else:
                logging.error("You need to define API_REPORTER_URL to use ApiReporter")

    # Send JSON log to remote api
    def produce_report(self):
        # Build payload
        self.build_payload()
        # Format payload according to target
        self.format_payload()
        # Call API
        self.send_to_api

    def build_payload(self):
        # Git info
        repo = git.Repo(os.getcwd())
        repo_name = repo.working_tree_dir.split("/")[-1]
        branch = repo.active_branch
        branch_name = branch.name
        self.payload = {
            "source": "MegaLinter",
            "gitRepoName": repo_name,
            "gitBranchName": branch_name,
            "gitIdentifier": f"${repo_name}/${branch_name}",
            # Org (must come from global variable)
            "orgIdentifier": config.get(
                self.master.request_id, "API_REPORTER_ORG_IDENTIFIER", ""
            ),
            "data": {},
            "linters": [],
        }
        for linter in self.master.linters:
            if linter.is_active is True:
                lang_lower = linter.descriptor_id.lower()
                linter_name_lower = linter.linter_name.lower().replace("-", "_")
                linter_doc_url = (
                    f"{ML_DOC_URL_DESCRIPTORS_ROOT}/{lang_lower}_{linter_name_lower}"
                )
                linter_payload = {
                    "descriptor": linter.descriptor_id,
                    "linter": linter.name,
                    "linterKey": linter.linter_name,
                    "linterDocUrl": linter_doc_url,
                    "data": {},
                }
                # Status
                linter_payload.severity = (
                    "Success"
                    if linter.status == "success" and linter.return_code == 0
                    else (
                        "Warning"
                        if linter.status != "success" and linter.return_code == 0
                        else "Error"
                    )
                )
                linter_payload.data.severityIcon = (
                    "✅"
                    if linter.status == "success" and linter.return_code == 0
                    else (
                        "⚠️"
                        if linter.status != "success" and linter.return_code == 0
                        else "❌"
                    )
                )
                # Number of files & errors
                linter_payload.data.cliLintMode = linter.cli_lint_mode
                if linter.cli_lint_mode != "project":
                    linter_payload.data.numberFilesFound = len(linter.files)
                linter_payload.data.numberErrorsFound = linter.total_number_errors
                # Fixed cells
                if linter.try_fix is True:
                    linter_payload.data.numberErrorsFixed = linter.number_fixed
                # Elapsed time
                if self.master.show_elapsed_time is True:
                    linter_payload.data.elapsedTime = round(linter.elapsed_time_s, 2)
                # Add to linters
                self.payload.linters.append(linter_payload)

    def format_payload(self):
        if "loki/api/v1/push" in self.api_url:
            self.format_payload_loki()
            return
        self.payloadFormatted = self.payload

    def format_payload_loki(self):
        time_ns = time.time_ns()
        streams = []
        for linter in self.payload.linters:
            payload_copy = copy.deepcopy(self.payload)
            del payload_copy.data
            del payload_copy.linters
            linter_copy = copy.deepcopy(linter)
            del linter_copy.data
            stream_info = payload_copy.update(payload_copy)
            data = copy.deepcopy(linter.data)
            data.update(self.payload.data)
            stream = {
                "stream": stream_info,
                "values": [[str(time_ns), json.dumps()]],
            }
            streams.append(stream)
        self.payloadFormatted = {"streams": streams}

    def send_to_api(self):
        session = requests.Session()
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        # Use username & password
        if config.exists(self.master.request_id, "API_REPORTER_BASIC_AUTH_USERNAME"):
            session.auth = (
                config.get(self.master.request_id, 'API_REPORTER_BASIC_AUTH_USERNAME'),
                config.get(self.master.request_id, 'API_REPORTER_BASIC_AUTH_PASSWORD')
            )
        # Use token
        if config.exists(self.master.request_id, "API_REPORTER_BEARER_TOKEN"):
            headers["Authorization"] = (
                f"Bearer {config.get(self.master.request_id, 'API_REPORTER_BEARER_TOKEN')}"
            )
        try:
            response = session.post(
                self.api_url, headers=headers, json=self.payloadFormatted
            )
            if 200 <= response.status_code < 300:
                logging.info(
                    f"[Api Reporter] Successfully posted data to {self.api_url}"
                )
            else:
                logging.warning(
                    f"[Api Reporter] Error posting data to {self.api_url} ({response.status_code})\n"
                    f"API response: {response.text}"
                )
        except ConnectionError as e:
            logging.warning(
                f"[Api Reporter] Error posting data to {self.api_url}:"
                f"Connection error {str(e)}"
            )
        except Exception as e:
            logging.warning(
                f"[Api Reporter] Error posting data to {self.api_url}:"
                f"Connection error {str(e)}"
            )
