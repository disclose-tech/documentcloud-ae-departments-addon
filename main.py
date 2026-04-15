from datetime import datetime, timedelta
import json
import logging
import os
import sys
import time

from documentcloud.addon import AddOn
from documentcloud.exceptions import APIError
from tenacity import retry, stop_after_attempt, wait_random_exponential, RetryError

from ai import get_departments_from_gpt4, MODEL_NAME
from corrections import corrections

BATCH_RESULTS_JSON_PATH = "data/20241209_batch_results_departments.json"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)
logger.setLevel(logging.DEBUG)
documentcloud_logger = logging.getLogger("documentcloud")
documentcloud_logger.setLevel(logging.WARNING)


class AEDepartmentsAddon(AddOn):

    start_time = datetime.now()

    processed_count = {
        "corrections": 0,
        "batch_results": 0,
        "event_data": 0,
        "ai": 0,
    }

    def get_project_id(self):
        """Returns the id of the target project."""

        project = self.data["project"]

        try:
            # if project is an integer, use it as a project ID
            project = int(project)
            return project
        except ValueError:
            # otherwise, get the project id from its title
            # or create it if it does not exist
            project, created = self.client.projects.get_or_create_by_title(project)
            return project.id

    def load_batch_results(self):

        if os.path.isfile(BATCH_RESULTS_JSON_PATH):
            with open(BATCH_RESULTS_JSON_PATH, "r") as batch_results_file:
                batch_results = json.load(batch_results_file)
                logger.info(f"Loaded batch results file {BATCH_RESULTS_JSON_PATH}.")

                batch_results[
                    "0180c60c60c22ed3e0f94c1d9f9798d922830cef7dc542d5d6194bd0b6259f1c"
                ] = ["test"]

            return batch_results
        else:
            return {}

    def load_or_create_event_data(self):

        event_data = self.load_event_data()
        if event_data:
            logger.info(
                f"Loaded event_data from DocumentCloud ({len(event_data)} entries)."
            )
            return event_data
        else:
            logger.info("No event_data loaded from DocumentCloud.")
            return {}

        return event_data

    def save_event_data(self):

        self.store_event_data(self.event_data)
        logger.info(
            f"Event data stored in DocumentCloud ({len(self.event_data)} entries)"
        )

        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M")
        filename = f"event_data_departments_classification_{timestamp}.json"
        with open(filename, "w+") as event_data_file:
            json.dump(self.event_data, event_data_file)
            self.upload_file(event_data_file)
            logger.info(
                f"Event data uploaded to DocumentCloud interface ({len(self.event_data)} entries)"
            )

        if not self.id:  # local run
            with open("event_data.json", "w") as event_data_file:
                json.dump(self.event_data, event_data_file)
            logger.info(
                f"Event data saved to event_data.json ({len(self.event_data)} entries)"
            )

    def set_end_message(self):
        total = (
            self.processed_count["ai"]
            + self.processed_count["batch_results"]
            + self.processed_count["corrections"]
            + self.processed_count["event_data"]
        )

        end_message = f"Categorized {total} documents."
        details = " | ".join(
            [f"{x}: {self.processed_count[x]}" for x in self.processed_count]
        )
        end_message += f" ({details})"

        if self.dry_run:
            end_message += " - (dry run)"

        self.set_message(end_message)
        logger.info(end_message)

    def close_addon(self):
        if not self.dry_run:
            self.save_event_data()
        self.set_end_message()
        sys.exit(0)

    def check_time_limit(self):

        logger.debug("Checking time limit...")
        if self.time_limit != 0:

            limit_seconds = self.time_limit * 60
            now = datetime.now()

            if timedelta.total_seconds(now - self.start_time) > limit_seconds:
                logger.info(f"Closing due to time limit ({self.time_limit} minutes).")
                self.close_addon()

    def check_ai_requests_limit(self):

        if self.processed_count["ai"] >= self.max_ai_requests:
            self.close_addon()

    def search_documents(self, search_string):

        @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
        def search(search_string):
            documents = self.client.documents.search(search_string)
            return documents

        logger.debug("Searching Documentcloud...")
        try:
            documents = search(search_string)
        except RetryError:
            logger.error("Failed searching DocumentCloud. Closing Add-On...")
            self.close_addon()

        return documents

    def tag_document(self, doc, departments, source):
        """Tag a document with departments."""
        if not self.dry_run:
            logger.debug(f"Tagging document {doc.id}")
            if departments:
                doc.data["departments"] = departments
                doc.data["departments_sources"] = [source]
            else:
                doc.data["departments_failed_sources"] = [source]
            doc.save()

    def main(self):

        # User agent
        self.client.session.headers.update(
            {"User-Agent": "Disclose AE Departments Add-On"}
        )

        # Inputs
        self.run_name = self.data.get("run_name", "no name")
        self.max_ai_requests = self.data.get("max_ai_requests", 10)
        self.time_limit = self.data.get("time_limit", 345)
        self.dry_run = self.data.get("dry_run")

        self.set_message(f"Running... [{self.run_name}]")

        try:
            self.project = self.get_project_id()
        except Exception as e:
            raise Exception("Project error").with_traceback(e.__traceback__)
            sys.exit(1)

        # batch results, event_data
        batch_results = self.load_batch_results()
        self.event_data = self.load_or_create_event_data()

        # Search docs that need to be classified
        documents = self.search_documents(
            f'+project:{str(self.project)} -data_departments:* +status:"success" -data_departments_failed_sources:"{MODEL_NAME}" sort:-data_publication_datetime'
        )
        logger.info(f"Found {documents.count} documents to classify.")

        # Loop
        try:
            for doc in documents:

                project_id = doc.data["project_id"][0]

                logger.debug(f"Processing doc {doc.id}, project {project_id}")

                # Check corrections
                if project_id in corrections:
                    logger.debug(
                        f"Document {doc.id} will be categorized from corrections."
                    )
                    self.tag_document(
                        doc=doc, departments=corrections[project_id], source="human"
                    )
                    if not self.dry_run:
                        self.processed_count["corrections"] += 1

                # Check batch results
                elif project_id in batch_results:
                    logger.debug(
                        f"Document {doc.id} will be categorized from batch results."
                    )
                    self.tag_document(
                        doc=doc,
                        departments=batch_results[project_id],
                        source=MODEL_NAME,
                    )
                    if not self.dry_run:
                        self.processed_count["batch_results"] += 1

                # Check event_data
                elif project_id in self.event_data:
                    logger.debug(
                        f"Document {doc.id} will be categorized from event data."
                    )
                    self.tag_document(
                        doc=doc,
                        departments=self.event_data[project_id],
                        source=MODEL_NAME,
                    )
                    if not self.dry_run:
                        self.processed_count["event_data"] += 1

                # Send to gpt
                else:
                    logger.debug(f"Document {doc.id} will be classified by GPT.")

                    # Find docs from the same project
                    project_documents = self.search_documents(
                        f'+project:{str(self.project)} +status:"success" +data_project_id:"{project_id}"'
                    )
                    logger.debug(
                        f"Its project has {project_documents.count} documents."
                    )

                    # If the document is..
                    if project_documents.count == 1:
                        # - alone in its project/dossier
                        if "form_error" in doc.data and doc.data["form_error"] == "yes":
                            # - tagged form_error:yes
                            if "ocr_engine" not in doc.data or doc.data[
                                "ocr_engine"
                            ] not in ["google", "azure", "textract"]:
                                # - and not OCR'd
                                logger.debug("Skipping document {doc.id}")
                                continue

                    if not self.dry_run:
                        departments_from_gpt = get_departments_from_gpt4(
                            project_documents
                        )

                        logger.debug("Storing departments in event_data...")
                        # Save to event_data first, regardless of whether there is a result
                        self.event_data[project_id] = departments_from_gpt

                        # Store event_data every run ?
                        logger.debug("Storing event data.")
                        self.store_event_data(self.event_data)

                        self.tag_document(
                            doc=doc,
                            departments=departments_from_gpt,
                            source=MODEL_NAME,
                        )
                        self.processed_count["ai"] += 1

                self.check_time_limit()
                self.check_ai_requests_limit()

                time.sleep(0.2)
                # End of loop

            logger.info("All documents have been processed. Closing Add-On.")
            self.close_addon()

        except APIError:
            logger.warning("DocumentCloud API Error, closing...")
            self.close_addon()


if __name__ == "__main__":
    AEDepartmentsAddon().main()
