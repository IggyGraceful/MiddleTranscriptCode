#qual_functions.py
import logging
from openai import OpenAI
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import faiss
import json
import numpy as np
import os
from pydantic import BaseModel
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)  # for exponential backoff

# Initialize OpenAI client
try:
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY environment variable is not set.")
    client = OpenAI(api_key=openai_api_key)
    logger = logging.getLogger(__name__)
except Exception as e:
    logging.getLogger(__name__).error(f"Failed to initialize OpenAI client: {e}")
    raise

#Retry functionality with Tenacity
@retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
def completion_with_backoff(**kwargs):
    return client.beta.chat.completions.parse(**kwargs)

# -------------------------------
# Data Classes
# -------------------------------

@dataclass
class CodeAssigned:
    code_name: str
    code_justification: str

@dataclass
class MeaningUnit:
    unique_id: int
    meaning_unit_string: str
    assigned_code_list: List[CodeAssigned] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unique_id": self.unique_id,
            "meaning_unit_string": self.meaning_unit_string,
            "assigned_code_list": [code.__dict__ for code in self.assigned_code_list],
            "metadata": self.metadata
        }

# -------------------------------
# Pydantic Models for GPT output format
# -------------------------------

class ParseFormat(BaseModel):
    meaning_unit_string_list: List[str]

class CodeFormat(BaseModel):
    codeList: List[CodeAssigned]

# -------------------------------
# Core Functions
# -------------------------------

def parse_transcript(
    speaking_turn_string: str, 
    prompt: str, 
    completion_model: str, 
    metadata: Dict[str, Any] = None
) -> List[str]:
    """
    Breaks up a speaking turn into smaller meaning units based on criteria in the LLM prompt.

    Args:
        speaking_turn_string (str): The speaking turn text.
        prompt (str): The prompt instructions for parsing.
        completion_model (str): The language model to use.
        metadata (Dict[str, Any], optional): Additional metadata.

    Returns:
        List[str]: A list of meaning units extracted from the speaking turn.
    """
    metadata_section = f"Metadata:\n{json.dumps(metadata, indent=2)}\n\n" if metadata else ""
    try:
        response = completion_with_backoff(
            model=completion_model,
            messages=[
                {
                    "role": "system", 
                    "content": "You are a qualitative research assistant that breaks down speaking turns into smaller meaning units based on given instructions."
                },
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\n"
                        f"{metadata_section}"
                        f"Speaking Turn:\n{speaking_turn_string}\n\n"
                    )
                }
            ],
            response_format=ParseFormat,
            temperature=0.2,
            max_tokens=1500,
        )
        
        if not response.choices:
            logger.error("No choices returned in the response from OpenAI API.")
            return []
        
        parsed_output = response.choices[0].message.parsed

        if not parsed_output:
            logger.error("Parsed output is empty.")
            return []

        # Validate the structure of parsed_output
        if not hasattr(parsed_output, 'meaning_unit_string_list'):
            logger.error("Parsed output does not contain required field 'meaning_unit_string_list'.")
            return []

        meaningunit_stringlist_parsed = parsed_output.meaning_unit_string_list

        if not isinstance(meaningunit_stringlist_parsed, list):
            logger.error("'meaning_unit_string_list' is not a list.")
            return []

        # Log the parsed meaning units
        logger.debug(f"Parsed Meaning Units: {meaningunit_stringlist_parsed}")

        return meaningunit_stringlist_parsed

    except Exception as e:
        logger.error(f"An error occurred while parsing transcript into meaning units: {e}")
        return []

def initialize_faiss_index_from_formatted_file(
    codes_list_file: str, 
    embedding_model: str = "text-embedding-3-small", 
    batch_size: int = 32
) -> Tuple[faiss.IndexFlatL2, List[Dict[str, Any]]]:
    """
    Reads a JSONL-formatted file, processes code data, and initializes a FAISS index directly using batch embedding.
    Returns the FAISS index and the processed codes as dictionaries.

    Args:
        codes_list_file (str): Path to the JSONL codebase file.
        embedding_model (str, optional): Embedding model to use.
        batch_size (int, optional): Number of items to process in each batch.

    Returns:
        Tuple[faiss.IndexFlatL2, List[Dict[str, Any]]]: FAISS index and processed codes.
    """
    embeddings = []
    processed_codes = []
    combined_texts = []  # For batch processing

    try:
        with open(codes_list_file, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue  # Skip empty lines

                # Parse each JSONL line
                data = json.loads(line)
                text = data.get("text", "")
                metadata = data.get("metadata", {})
                
                processed_code = {
                    'text': text,
                    'metadata': metadata
                }
                processed_codes.append(processed_code)

                # Combine `text` and metadata elements for embedding
                combined_text = f"{text} Metadata: {metadata}"
                combined_texts.append(combined_text)

                # If batch size is reached, process the batch
                if len(combined_texts) == batch_size:
                    response = client.embeddings.create(
                        input=combined_texts,
                        model=embedding_model
                    )
                    batch_embeddings = [res.embedding for res in response.data]
                    embeddings.extend(batch_embeddings)

                    # Reset for the next batch
                    combined_texts = []

            # Process any remaining texts in the last batch
            if combined_texts:
                response = client.embeddings.create(
                    input=combined_texts,
                    model=embedding_model
                )
                batch_embeddings = [res.embedding for res in response.data]
                embeddings.extend(batch_embeddings)

        # Convert embeddings to numpy array
        embeddings = np.array(embeddings).astype('float32')

        # Initialize FAISS index
        if embeddings.size > 0:
            dimension = len(embeddings[0])
            index = faiss.IndexFlatL2(dimension)
            index.add(embeddings)
        else:
            raise ValueError("No valid embeddings found. Check the content of your JSONL file.")

        logger.info(f"Initialized FAISS index with {len(processed_codes)} codes from file '{codes_list_file}'.")
        return index, processed_codes

    except Exception as e:
        logger.error(f"An error occurred while processing the file '{codes_list_file}' and initializing FAISS index: {e}")
        raise e

def retrieve_relevant_codes(
    meaning_unit: MeaningUnit, 
    index: faiss.IndexFlatL2, 
    processed_codes: List[Dict[str, Any]], 
    top_k: int = 5, 
    embedding_model: str = "text-embedding-3-small"
) -> List[Dict[str, Any]]:
    """
    Retrieves the top_k most relevant codes for a given meaning_unit_string using FAISS.
    Returns a list of code dictionaries with relevant information.

    Args:
        meaning_unit (MeaningUnit): The meaning unit object.
        index (faiss.IndexFlatL2): The FAISS index.
        processed_codes (List[Dict[str, Any]]): List of processed codes.
        top_k (int, optional): Number of top similar codes to retrieve.
        embedding_model (str, optional): Embedding model to use.

    Returns:
        List[Dict[str, Any]]: List of relevant code dictionaries.
    """
    try:
        # Combine metadata and meaning unit string for embedding
        meaning_unit_string_with_metadata = f"Metadata:\n{json.dumps(meaning_unit.metadata)}\nUnit: {meaning_unit.meaning_unit_string}"

        response = client.embeddings.create(
            input=[meaning_unit_string_with_metadata],
            model=embedding_model
        )
        if not response.data:
            logger.error("No embedding data returned from OpenAI API.")
            return []
        meaning_unit_embedding = np.array([response.data[0].embedding]).astype('float32')

        distances, indices = index.search(meaning_unit_embedding, top_k)
        relevant_codes = [processed_codes[idx] for idx in indices[0] if idx < len(processed_codes)]

        code_names = [code.get('text', 'Unnamed Code') for code in relevant_codes]
        logger.debug(f"Retrieved top {top_k} relevant codes for meaning unit ID {meaning_unit.unique_id}: {code_names}")
        return relevant_codes

    except Exception as e:
        logger.error(f"An error occurred while retrieving relevant codes: {e}")
        return []

def assign_codes_to_meaning_units(
    meaning_unit_list: List[MeaningUnit], 
    coding_instructions: str, 
    processed_codes: Optional[List[Dict[str, Any]]] = None, 
    index: Optional[faiss.IndexFlatL2] = None, 
    top_k: Optional[int] = 5,
    context_size: int = 5,
    use_rag: bool = True,
    codebase: Optional[List[Dict[str, Any]]] = None,
    completion_model: Optional[str] = "gpt-4o-mini",
    embedding_model: Optional[str] = "text-embedding-3-small"
) -> List[MeaningUnit]:
    """
    Assigns codes to each MeaningUnit object, including contextual information from surrounding units.
    Returns an updated list of MeaningUnit objects with assigned codes.

    Args:
        meaning_unit_list (List[MeaningUnit]): List of meaning units to process.
        coding_instructions (str): Coding instructions prompt.
        processed_codes (Optional[List[Dict[str, Any]]], optional): List of processed codes for deductive coding.
        index (Optional[faiss.IndexFlatL2], optional): FAISS index for RAG.
        top_k (Optional[int], optional): Number of top similar codes to retrieve.
        context_size (int, optional): Number of surrounding meaning units to include as context.
        use_rag (bool, optional): Whether to use RAG for code retrieval.
        codebase (Optional[List[Dict[str, Any]]], optional): Entire codebase for deductive coding without RAG.
        completion_model (Optional[str], optional): Language model to use for code assignment.
        embedding_model (Optional[str], optional): Embedding model to use for code retrieval.

    Returns:
        List[MeaningUnit]: Updated list with assigned codes.
    """
    try:
        total_units = len(meaning_unit_list)
        for idx, meaning_unit_object in enumerate(meaning_unit_list):
            # Determine coding approach
            is_deductive = processed_codes is not None

            if is_deductive and use_rag and index is not None:
                # Retrieve relevant codes using FAISS and the specified embedding model
                relevant_codes = retrieve_relevant_codes(
                    meaning_unit_object, 
                    index, 
                    processed_codes, 
                    top_k=top_k,
                    embedding_model=embedding_model
                )
                codes_to_include = relevant_codes
            elif is_deductive and not use_rag and codebase:
                # Deductive coding without RAG, using entire codebase
                codes_to_include = codebase
            else:
                # Inductive coding: No predefined codes
                codes_to_include = None

            # Format codes or guidelines as a string
            if codes_to_include is not None:
                # Deductive coding: format codes as a string
                codes_str = "\n\n".join([json.dumps(code, indent=2) for code in codes_to_include])
            else:
                # Inductive coding: Provide only the guidelines
                codes_str = "No predefined codes. Please generate codes based on the following guidelines."

            # Retrieve context for the current meaning unit
            context_excerpt = ""

            # Collect previous units for context
            if context_size > 0 and idx > 0:
                prev_units = meaning_unit_list[max(0, idx - context_size):idx]
                for unit in prev_units:
                    context_excerpt += (
                        f"Quote: {unit.meaning_unit_string}\n\n"
                    )

            # Add current excerpt to context
            current_unit_excerpt = (
                f"Quote: {meaning_unit_object.meaning_unit_string}\n\n"
            )

            context_excerpt += current_unit_excerpt

            # Collect following units for context
            if context_size > 0 and idx < total_units - 1:
                next_units = meaning_unit_list[idx + 1: idx + 1 + context_size]
                for unit in next_units:
                    context_excerpt += (
                        f"Quote: {unit.meaning_unit_string}\n\n"
                    )

            # Construct the full prompt
            if codes_to_include is not None:
                code_heading = "Relevant Codes (full details):" if use_rag else "Full Codebase (all codes with details):"
            else:
                code_heading = "Guidelines for Inductive Coding:"

            full_prompt = (
                f"{coding_instructions}\n\n"
                f"{code_heading}\n{codes_str}\n\n"
                f"Contextual Excerpts:\n{context_excerpt}"
                f"**Important:** Please use the provided contextual excerpts **only** as background information to understand the current excerpt better. "
                f"Current Excerpt For Coding:\n{current_unit_excerpt}"
                f"{'**Apply codes exclusively to the current excerpt provided above. Do not assign codes to the contextual excerpts.**' if codes_to_include is not None else '**Generate codes based on the current excerpt provided above using the guidelines.**'}\n\n"
                f"Please provide the assigned codes in the following JSON format:\n"
                f"{{\n  \"codeList\": [\n    {{\"code_name\": \"<Name of the code>\", \"code_justification\": \"<Justification for the code>\"}},\n    ...\n  ]\n}}"
            )

            logger.debug(f"Full Prompt for Unique ID {meaning_unit_object.unique_id}:\n{full_prompt}")

            try:
                response = completion_with_backoff(
                    model=completion_model,
                    messages=[
                        {
                            "role": "system", 
                            "content": (
                                "You are tasked with applying qualitative codes to excerpts from a transcript or articles. "
                                "The purpose of this task is to identify all codes that best describe each excerpt based on the provided instructions."
                            )
                        },
                        {
                            "role": "user",
                            "content": full_prompt
                        }
                    ],
                    response_format=CodeFormat,
                    temperature=0.2,
                    max_tokens=1500,
                )

                if not response.choices:
                    logger.error(f"No choices returned for Unique ID {meaning_unit_object.unique_id}.")
                    continue

                code_output = response.choices[0].message.parsed
                logger.debug(f"LLM Code Assignment Output for ID {meaning_unit_object.unique_id}:\n{code_output.codeList}")

                # Append each code_name and code_justification to the meaning_unit_object
                for code_item in code_output.codeList:
                    code_name = getattr(code_item, 'code_name', 'Unknown Code')
                    code_justification = getattr(code_item, 'code_justification', 'No justification provided')
                    meaning_unit_object.assigned_code_list.append(
                        CodeAssigned(code_name=code_name, code_justification=code_justification)
                    )

            except Exception as e:
                logger.error(f"An error occurred while retrieving code assignments for Unique ID {meaning_unit_object.unique_id}: {e}")

    except Exception as e:
        logger.error(f"An error occurred while assigning codes: {e}")

    return meaning_unit_list
