# qual_functions.py
import logging
from openai import OpenAI
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import faiss
import json
import numpy as np
import os
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)

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

# Retry functionality with Tenacity
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
class SpeakingTurn:
    source_id: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        metadata = self.metadata.copy()
        # Ensure source_id is not duplicated in metadata
        metadata.pop('source_id', None)
        return {
            "source_id": self.source_id,
            "content": self.content,
            "metadata": metadata
        }

@dataclass
class MeaningUnit:
    meaning_unit_id: int
    meaning_unit_string: str
    assigned_code_list: List[CodeAssigned] = field(default_factory=list)
    speaking_turn: SpeakingTurn = None  # Link to associated SpeakingTurn

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meaning_unit_id": self.meaning_unit_id,
            "meaning_unit_string": self.meaning_unit_string,
            "assigned_code_list": [code.__dict__ for code in self.assigned_code_list],
            "speaking_turn": self.speaking_turn.to_dict() if self.speaking_turn else None
        }

# -------------------------------
# Pydantic Models for GPT Output Format
# -------------------------------

class ParseFormat(BaseModel):
    source_id: str  # Ensure source_id is string
    quote: str

class ParseResponse(BaseModel):
    parse_list: List[ParseFormat]

class CodeAssignmentResponse(BaseModel):
    meaning_unit_id: int
    codeList: List[CodeAssigned]

class CodeResponse(BaseModel):
    assignments: List[CodeAssignmentResponse]

# -------------------------------
# Core Functions
# -------------------------------

def parse_transcript(
    speaking_turns: List[Dict[str, Any]],
    prompt: str,
    completion_model: str
) -> List[Tuple[str, str]]:
    """
    Breaks up multiple speaking turns into smaller meaning units based on criteria in the LLM prompt.
    Returns a list of tuples containing source_id and the meaning unit string.

    Args:
        speaking_turns (List[Dict[str, Any]]): A list of speaking turns with metadata, including source_id.
        prompt (str): The prompt instructions for parsing.
        completion_model (str): The language model to use.

    Returns:
        List[Tuple[str, str]]: A list of tuples where each tuple contains the source_id and a meaning unit string.
    """
    try:
        response = completion_with_backoff(
            model=completion_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a qualitative research assistant that breaks down multiple speaking turns into smaller meaning units based on given instructions."
                },
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\n"
                        f"Speaking Turns:\n{json.dumps(speaking_turns, indent=2)}\n\n"
                    )
                }
            ],
            response_format=ParseResponse,
            temperature=0.2,
            max_tokens=16000,
        )

        if not response.choices:
            logger.error("No choices returned in the response from OpenAI API.")
            return []

        parsed_output = response.choices[0].message.parsed

        if not parsed_output:
            logger.error("Parsed output is empty.")
            return []

        # Access the root list from ParseResponse
        if not isinstance(parsed_output, ParseResponse):
            logger.error("Parsed output is not an instance of ParseResponse.")
            return []

        meaning_units = []
        for unit in parsed_output.parse_list:
            if not isinstance(unit, ParseFormat):
                logger.warning(f"Unit is not of type ParseFormat: {unit}")
                continue
            meaning_units.append((unit.source_id, unit.quote))

        # Log the parsed meaning units
        logger.debug(f"Parsed Meaning Units: {meaning_units}")

        return meaning_units

    except ValidationError as ve:
        logger.error(f"Validation error while parsing transcript: {ve}")
        return []
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

def retrieve_relevant_codes_batch(
    meaning_units: List['MeaningUnit'],
    index: faiss.IndexFlatL2,
    processed_codes: List[Dict[str, Any]],
    top_k: int = 5,
    embedding_model: str = "text-embedding-3-small"
) -> List[Dict[str, Any]]:
    """
    Retrieves the top_k most relevant codes for a batch of meaning units using multi-vector search.
    Returns a list of code dictionaries with relevant information.

    Args:
        meaning_units (List[MeaningUnit]): The meaning unit objects.
        index (faiss.IndexFlatL2): The FAISS index.
        processed_codes (List[Dict[str, Any]]): List of processed codes.
        top_k (int, optional): Number of top similar codes to retrieve per meaning unit.
        embedding_model (str, optional): Embedding model to use.

    Returns:
        List[Dict[str, Any]]: List of relevant code dictionaries.
    """
    try:
        # Combine meaning unit strings and metadata
        combined_texts = []
        for mu in meaning_units:
            meaning_unit_string_with_metadata = f"Metadata:\n{json.dumps(mu.speaking_turn.metadata)}\nUnit: {mu.meaning_unit_string}"
            combined_texts.append(meaning_unit_string_with_metadata)

        # Get embeddings for all meaning units in the batch
        response = client.embeddings.create(
            input=combined_texts,
            model=embedding_model
        )
        if not response.data:
            logger.error("No embedding data returned from OpenAI API.")
            return []
        meaning_unit_embeddings = np.array([res.embedding for res in response.data]).astype('float32')

        # Perform similarity search for each embedding
        all_indices = []
        for embedding in meaning_unit_embeddings:
            distances, indices = index.search(np.array([embedding]), top_k)
            all_indices.extend(indices[0].tolist())

        # Remove duplicates
        unique_indices = list(set(all_indices))

        # Collect relevant codes
        relevant_codes = [processed_codes[idx] for idx in unique_indices if idx < len(processed_codes)]
        code_names = [code.get('text', 'Unnamed Code') for code in relevant_codes]
        logger.debug(f"Retrieved relevant codes for batch: {code_names}")
        return relevant_codes
    except Exception as e:
        logger.error(f"An error occurred while retrieving relevant codes for batch: {e}")
        return []

def assign_codes_to_meaning_units(
    meaning_unit_list: List['MeaningUnit'],
    coding_instructions: str,
    processed_codes: Optional[List[Dict[str, Any]]] = None,
    index: Optional[faiss.IndexFlatL2] = None,
    top_k: Optional[int] = 5,
    context_size: int = 5,
    use_rag: bool = True,
    codebase: Optional[List[Dict[str, Any]]] = None,
    completion_model: Optional[str] = "gpt-4o-mini",
    embedding_model: Optional[str] = "text-embedding-3-small",
    meaning_units_per_assignment_prompt: int = 1,
    speaker_field: Optional[str] = None,
    content_field: str = 'content',
    full_speaking_turns: Optional[List[Dict[str, Any]]] = None
) -> List['MeaningUnit']:
    """
    Assigns codes to each MeaningUnit object, including contextual information from subsequent speaking turns.
    Processes multiple meaning units per prompt based on configuration.

    Args:
        meaning_unit_list (List[MeaningUnit]): List of meaning units to process.
        coding_instructions (str): Coding instructions prompt.
        processed_codes (Optional[List[Dict[str, Any]]], optional): List of processed codes for deductive coding.
        index (Optional[faiss.IndexFlatL2], optional): FAISS index for RAG.
        top_k (Optional[int], optional): Number of top similar codes to retrieve.
        context_size (int, optional): Number of subsequent speaking turns to include as context.
        use_rag (bool, optional): Whether to use RAG for code retrieval.
        codebase (Optional[List[Dict[str, Any]]], optional): Entire codebase for deductive coding without RAG.
        completion_model (Optional[str], optional): Language model to use for code assignment.
        embedding_model (Optional[str]): Embedding model to use for code retrieval.
        meaning_units_per_assignment_prompt (int, optional): Number of meaning units to process per assignment prompt.
        speaker_field (Optional[str], optional): The field name for speaker information.
        content_field (str, optional): The field name for content in full speaking turns.
        full_speaking_turns (Optional[List[Dict[str, Any]]], optional): List of pre-filtered speaking turns.

    Returns:
        List[MeaningUnit]: Updated list with assigned codes.
    """
    try:
        if full_speaking_turns is None:
            logger.error("full_speaking_turns must be provided for context.")
            raise ValueError("full_speaking_turns must be provided for context.")

        # Create a mapping from source_id to index in full_speaking_turns
        source_id_to_index = {str(d.get('source_id')): idx for idx, d in enumerate(full_speaking_turns)}  # Ensure keys are strings

        total_units = len(meaning_unit_list)
        for i in range(0, total_units, meaning_units_per_assignment_prompt):
            batch = meaning_unit_list[i:i + meaning_units_per_assignment_prompt]

            # Determine coding approach for the batch
            is_deductive = processed_codes is not None

            if is_deductive and use_rag and index is not None:
                # Retrieve relevant codes for the batch using multi-vector search
                relevant_codes = retrieve_relevant_codes_batch(
                    meaning_units=batch,
                    index=index,
                    processed_codes=processed_codes,
                    top_k=top_k,
                    embedding_model=embedding_model
                )
                # Ensure no duplicates
                codes_to_include = relevant_codes
            elif is_deductive and not use_rag and codebase:
                # Deductive coding without RAG, using entire codebase
                codes_to_include = codebase
            else:
                # Inductive coding: No predefined codes
                codes_to_include = None

            # Prepare context for the batch
            batch_context = ""
            added_speaking_turns = set()

            # Get the source_id of the first meaning unit in the batch
            first_unit = batch[0]
            first_source_id = first_unit.speaking_turn.source_id

            # Find the index in full_speaking_turns
            idx = source_id_to_index.get(first_source_id)
            if idx is not None:
                # Get context_size speaking turns starting from idx
                end_context_idx = min(len(full_speaking_turns), idx + context_size)
                context_speaking_turns = full_speaking_turns[idx:end_context_idx]
                for st in context_speaking_turns:
                    st_source_id = str(st.get('source_id'))
                    if st_source_id not in added_speaking_turns:
                        speaker = st.get(speaker_field, "Unknown Speaker") if speaker_field else "Unknown Speaker"
                        content = st.get(content_field, "")
                        batch_context += f"Source ID: {st_source_id}\nSpeaker: {speaker}\n{content}\n\n"
                        added_speaking_turns.add(st_source_id)
            else:
                logger.warning(f"Source ID {first_source_id} not found in full speaking turns.")

            # Construct the prompt for the batch
            full_prompt = f"{coding_instructions}\n\n"

            if codes_to_include is not None:
                # Collect unique codes
                unique_codes_set = set(json.dumps(code, indent=2) for code in codes_to_include)
                codes_str = "\n\n".join(unique_codes_set)
                code_heading = "Relevant Codes (full details):" if use_rag else "Full Codebase (all codes with details):"
                full_prompt += f"{code_heading}\n{codes_str}\n\n"
            else:
                codes_str = "No predefined codes. Please generate codes based on the following guidelines."
                code_heading = "Guidelines for Inductive Coding:"
                full_prompt += f"{code_heading}\n{codes_str}\n\n"

            # Include context once per batch
            full_prompt += (
                f"Contextual Excerpts (Starting from Source ID {first_source_id}):\n{batch_context}\n\n"
                f"**Important:** Please use the provided contextual excerpts **only** as background information to understand the current excerpt better. "
            )

            for unit in batch:
                speaker = unit.speaking_turn.metadata.get(speaker_field, "Unknown Speaker") if speaker_field else "Unknown Speaker"
                current_unit_excerpt = f"Quote: {unit.meaning_unit_string}\n\n"
                full_prompt += (
                    f"Current Excerpt For Coding (Meaning Unit ID {unit.meaning_unit_id}) Speaker: {speaker}:\n{current_unit_excerpt}"
                )

            full_prompt += (
                f"{'**Apply codes exclusively to the current excerpt provided above. Do not assign codes to the contextual excerpts.**' if codes_to_include is not None else '**Generate codes based on the current excerpt provided above using the guidelines.**'}\n\n"
                f"Please provide the assigned codes for the meaning unit(s) above in the following JSON format:\n"
                f"{{\n  \"assignments\": [\n    {{\n      \"meaning_unit_id\": <Meaning Unit ID>,\n      \"codeList\": [\n        {{\"code_name\": \"<Name of the code>\", \"code_justification\": \"<Justification for the code>\"}},\n        ...\n      ]\n    }},\n    ...\n  ]\n}}\n\n"
            )

            logger.debug(f"Full Prompt for Batch starting at index {i}:\n{full_prompt}")

            try:
                response = completion_with_backoff(
                    model=completion_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are tasked with applying qualitative codes to excerpts from transcripts or articles. "
                                "The purpose of this task is to identify all codes that best describe each excerpt based on the provided instructions."
                            )
                        },
                        {
                            "role": "user",
                            "content": full_prompt
                        }
                    ],
                    response_format=CodeResponse,
                    temperature=0.2,
                    max_tokens=16000,  # Increased tokens to accommodate multiple responses
                )

                if not response.choices:
                    logger.error(f"No choices returned for batch starting at index {i}.")
                    continue

                code_output = response.choices[0].message.parsed

                if not isinstance(code_output, CodeResponse):
                    logger.error(f"Response for batch starting at index {i} is not of type CodeResponse.")
                    continue

                for assignment in code_output.assignments:
                    # Find the corresponding meaning unit
                    matching_units = [mu for mu in batch if mu.meaning_unit_id == assignment.meaning_unit_id]
                    if not matching_units:
                        logger.warning(f"No matching meaning unit found for meaning_unit_id {assignment.meaning_unit_id}.")
                        continue
                    meaning_unit = matching_units[0]

                    for code_item in assignment.codeList:
                        code_name = getattr(code_item, 'code_name', 'Unknown Code')
                        code_justification = getattr(code_item, 'code_justification', 'No justification provided')
                        meaning_unit.assigned_code_list.append(
                            CodeAssigned(code_name=code_name, code_justification=code_justification)
                        )

            except ValidationError as ve:
                logger.error(f"Validation error while assigning codes for batch starting at index {i}: {ve}")
                continue
            except Exception as e:
                logger.error(f"An error occurred while assigning codes for batch starting at index {i}: {e}")
                continue

    except Exception as e:
        logger.error(f"An error occurred while assigning codes: {e}")

    return meaning_unit_list
