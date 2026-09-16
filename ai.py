import os
import json
import logging
import time

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential, RetryError

from departments import get_all_department_codes, get_department_codes_from_authority


MODEL_NAME = "gpt-4o"

# Max number of pages to process for each document
MAX_PAGES = 4

SYSTEM_PROMPT = "Tu es un super assistant spécialisé dans les projets soumis à autorisation environnementale en France"

USER_PROMPT_START = """
Tu trouveras ci-dessous le texte d'un ou plusieurs documents qui se rapportent tous à un même projet, plan ou programme potentiellement soumis à autorisation environnementale en France, intitulé "{project_name}"

Sur la base du titre du projet et de ses documents, tu dois lister le ou les départements impactés par le projet dont il est question. Si le projet concerne toute une région, liste les départements composant cette région.
Utilise le code INSEE des départements. Pour les départements ou collectivités d'outre mer, utilise les codes suivants : "Guadeloupe": "971", Martinique": "972", Guyane": "973", "La Réunion": "974", "Saint-Pierre-et-Miquelon": "975", "Mayotte": "976", "Saint-Martin": "978",
"""

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

client = OpenAI(api_key=os.environ["TOKEN"])


def make_schema(authority):
    """Prepare schema for custom response format."""
    department_codes_list = get_department_codes_from_authority(authority)

    schema = {
        "name": "departments_response",
        "schema": {
            "type": "object",
            "properties": {
                "departements": {
                    "type": "array",
                    "description": "Liste des codes des départements impactés par le projet",
                    "items": {"enum": department_codes_list},
                },
            },
            "required": ["departements"],
            "additionalProperties": False,
            "strict": True,
        },
    }

    response_json_schema = {
        "type": "json_schema",
        "json_schema": schema,
    }

    return response_json_schema


def make_user_prompt(project_docs):
    """Prepare the prompt from documents pertaining to the same project"""

    documents_for_prompt = []

    for index, doc in enumerate(project_docs):

        doc_pages_content = []

        for page in range(min(MAX_PAGES, doc.pages)):

            doc_pages_content.append(doc.get_page_text(page + 1))
            time.sleep(0.3)

        doc_content = "\n".join(doc_pages_content)

        document_string = f"""
            ---- Début du document n°{index+1} ----
            {doc_content}
            ---- Fin du document n°{index+1} ----
            """

        documents_for_prompt.append(document_string)

    full_prompt = USER_PROMPT_START.format(
        project_name=project_docs[0].description,
        authority=project_docs[0].data["authority"][0],
    ) + "\n".join(documents_for_prompt)

    return full_prompt


# @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
def get_completion(user_prompt, response_json_schema):

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        max_tokens=500,
        temperature=0,
        response_format=response_json_schema,
    )

    result = response.choices[0].message.content.strip()

    return result


def get_departments_from_gpt4(project_docs):
    """Get the departments impacted by the project from OpenAI"""

    user_prompt = make_user_prompt(project_docs)

    authority = project_docs[0].data["authority"][0]
    response_json_schema = make_schema(authority)

    try:
        result = get_completion(user_prompt, response_json_schema)
    except RetryError:
        logger.error(f"Error: failed to get a response from the OpenAI API.")
        result = '{"departements": []}'

    try:
        result_dict = json.loads(result)
        departments_results = result_dict["departements"]
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON response from OpenAI API.")
        departments_results = []

    # Remove duplicates
    departments_results = list(set(departments_results))

    # Double check departments codes
    departments_codes_list = get_department_codes_from_authority(authority)
    departments_result_checked = [
        x for x in departments_results if x in departments_codes_list
    ]

    return sorted(departments_result_checked)
