import os
import logging
import aiohttp
import json
import asyncio
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path # Keep Path for type hinting if used elsewhere, but not for output_dir here

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://health.api.nvidia.com/v1/biology/deepmind/alphafold2-multimer"
DEFAULT_STATUS_URL = "https://health.api.nvidia.com/v1/status"

# _split_pdb_content remains the same
def _split_pdb_content(concatenated_pdb_str: str) -> List[str]:
    """
    Splits a string containing concatenated PDB file contents.
    Assumes models are separated by "ENDMDL" or just "END" for the last/single model.
    """
    pdbs = []
    current_pdb_lines = []
    if not concatenated_pdb_str:
        return []

    for line in concatenated_pdb_str.splitlines(keepends=True):
        current_pdb_lines.append(line)
        if line.startswith("ENDMDL") or line.startswith("END "): # Fixed: added space for "END "
            pdbs.append("".join(current_pdb_lines).strip())
            current_pdb_lines = []

    if current_pdb_lines:
        remaining_pdb = "".join(current_pdb_lines).strip()
        if remaining_pdb:
            pdbs.append(remaining_pdb)

    return [pdb for pdb in pdbs if pdb]


# calculate_plddt_from_pdb_string remains the same
def calculate_plddt_from_pdb_string(pdb_string: str) -> Tuple[float, List[float], Dict[str, List[float]]]:
    total_plddt = 0.0
    ca_atom_count = 0
    plddt_scores_per_ca: List[float] = []
    plddt_scores_per_chain: Dict[str, List[float]] = {}

    for line in pdb_string.splitlines():
        if line.startswith("ATOM"):
            atom_name = line[12:16].strip()
            if atom_name == "CA":
                try:
                    plddt_value = float(line[60:66].strip())
                    total_plddt += plddt_value
                    plddt_scores_per_ca.append(plddt_value)
                    ca_atom_count += 1

                    chain_id = line[21:22].strip()
                    if chain_id not in plddt_scores_per_chain:
                        plddt_scores_per_chain[chain_id] = []
                    plddt_scores_per_chain[chain_id].append(plddt_value)

                except ValueError:
                    pass
                except IndexError:
                    pass

    if ca_atom_count == 0:
        return 0.0, [], {}

    average_plddt = total_plddt / ca_atom_count
    return average_plddt, plddt_scores_per_ca, plddt_scores_per_chain

async def _process_pdb_and_scores_from_api(
    pdb_contents: List[str], # This is the list of PDB strings from the API
    job_id: str,
    api_response_json: Optional[Dict[str, Any]] = None # Not currently used, but kept for consistency
) -> Optional[Dict[str, Any]]: # Return structure: {"structures": [...]}
    """
    Processes a list of PDB strings received from the API.
    - Calculates pLDDT scores for each PDB string.
    - Does NOT save files to disk.
    - Returns a dictionary containing a list of structures, each with its PDB content and scores.
    """
    results: Dict[str, Any] = {
        "structures": []
    }

    if not pdb_contents or not isinstance(pdb_contents, list) or not all(isinstance(s, str) for s in pdb_contents):
        logger.warning(f"No valid PDB content strings provided for job {job_id}.")
        # Return a structure indicating failure or empty results, consistent with how call_alphafold2_multimer handles errors
        return {"success": False, "error": "No valid PDB content strings from API.", "structures": []}


    logger.info(f"Processing {len(pdb_contents)} PDB structure(s) for job {job_id}.")

    for i, pdb_str in enumerate(pdb_contents):
        if not pdb_str.strip():
            logger.debug(f"Skipping empty PDB string at index {i} for job {job_id}.")
            continue

        structure_data: Dict[str, Any] = {
            "model_index": i, # Keep model_index for identification
            "pdb_content": pdb_str # Store the actual PDB content
        }

        avg_plddt, plddts_per_ca_residue, plddts_by_chain = calculate_plddt_from_pdb_string(pdb_str)

        structure_data["average_plddt"] = avg_plddt
        structure_data["plddt_scores_per_ca_residue"] = plddts_per_ca_residue
        structure_data["plddt_scores_per_chain"] = plddts_by_chain

        avg_plddt_per_chain = {}
        for chain_id, chain_plddts in plddts_by_chain.items():
            if chain_plddts:
                avg_plddt_per_chain[chain_id] = sum(chain_plddts) / len(chain_plddts)
            else:
                avg_plddt_per_chain[chain_id] = 0.0
        structure_data["average_plddt_per_chain"] = avg_plddt_per_chain
        
        # REMOVED FILE SAVING LOGIC HERE
        # structure_data["saved_pdb_path"] = ... (NO LONGER SET HERE)

        results["structures"].append(structure_data)

    if results["structures"]:
         logger.info(f"Successfully processed and calculated pLDDTs for {len(results['structures'])} structures for job {job_id}.")
    else:
        logger.warning(f"No structures were processed for job {job_id}.")
        # Ensure a consistent return structure even if no structures processed
        return {"success": True, "message": "No PDB structures found in API response to process.", "structures": []}


    return results # This will contain {"structures": [...list of dicts with pdb_content and scores...]}

async def call_alphafold2_multimer(
    sequences: List[str],
    api_key: str,
    algorithm: str = "jackhmmer",
    e_value: float = 0.0001,
    iterations: int = 1,
    databases: List[str] = ["uniref90", "small_bfd", "mgnify"],
    relax_prediction: bool = True,
    selected_models: Optional[List[int]] = None,
    url: str = DEFAULT_URL,
    status_url: str = DEFAULT_STATUS_URL,
    polling_interval: int = 30,
    timeout: int = 3600
    # REMOVED output_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """
    Call the NVIDIA NIM AlphaFold2-Multimer API.
    The API returns JSON with a list of PDB strings.
    This function processes them to calculate pLDDT scores and returns a dictionary
    containing a list of structures, each with its PDB content and computed scores.
    File saving is handled by the caller (ToolExecutor).
    """
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "NVCF-POLL-SECONDS": "300",
    }
    data: Dict[str, Any] = {
        "sequences": sequences,
        "algorithm": algorithm,
        "e_value": e_value,
        "iterations": iterations,
        "databases": databases,
        "relax_prediction": relax_prediction
    }
    if selected_models is not None:
        data["selected_models"] = selected_models
        logger.info(f"Using selected_models: {selected_models}")

    try:
        initial_post_timeout = min(timeout, 600)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=data,
                headers=headers,
                timeout=initial_post_timeout
            ) as response:
                if response.status == 200:
                    logger.info("AlphaFold2-Multimer job completed synchronously.")
                    content_type = response.headers.get("Content-Type", "").lower()

                    if "application/json" in content_type:
                        api_response_json_payload = await response.json()
                        if not isinstance(api_response_json_payload, list):
                            # Handle case where API might return {"error": ...} directly in a sync 200
                            if isinstance(api_response_json_payload, dict) and "error" in api_response_json_payload:
                                logger.error(f"Sync API call returned error: {api_response_json_payload['error']}")
                                return {"success": False, "error": api_response_json_payload['error'], "detail": api_response_json_payload.get("detail","")}
                            return {"success": False, "error": "Sync JSON response not a list of PDBs as expected."}
                        
                        req_id_sync = response.headers.get("nvcf-reqid", "sync_job")
                        return await _process_pdb_and_scores_from_api( # No output_dir
                            pdb_contents=api_response_json_payload,
                            job_id=req_id_sync,
                            api_response_json=None # Not used currently
                        )
                    else:
                        err_text = await response.text()
                        logger.error(f"Sync response unexpected content type: {content_type}. Response: {err_text[:500]}")
                        return {"success": False, "error": f"Sync response unexpected content type: {content_type}", "detail": err_text}

                elif response.status == 202:
                    req_id = response.headers.get("nvcf-reqid")
                    if req_id:
                        logger.info(f"AlphaFold2-Multimer job submitted, request ID: {req_id}")
                        return await _poll_job_status( # No output_dir
                            req_id=req_id,
                            headers=headers,
                            status_url=status_url,
                            polling_interval=polling_interval,
                            overall_timeout=timeout
                        )
                    else:
                        logger.error("No request ID in 202 response headers")
                        return {"success": False, "error": "No request ID in 202 response headers"}
                else:
                    logger.error(f"Error calling AlphaFold2-Multimer API (POST): {response.status}")
                    text = await response.text()
                    logger.error(f"Response: {text}")
                    return {"success": False, "error": f"Error calling API: {response.status}", "detail": text}
    except asyncio.TimeoutError:
        logger.error(f"Timeout during AlphaFold2-Multimer API (initial POST).")
        return {"success": False, "error": "Timeout during initial API request"}
    except Exception as e:
        logger.error(f"Exception during AlphaFold2-Multimer API call (initial POST): {e}", exc_info=True)
        return {"success": False, "error": f"Exception during API call: {str(e)}"}

async def _poll_job_status(
    req_id: str,
    headers: Dict[str, str],
    status_url: str,
    polling_interval: int = 30,
    overall_timeout: int = 3600
    # REMOVED output_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    start_time = asyncio.get_event_loop().time()
    per_status_request_timeout = 600
    logger.info(f"Polling job {req_id}. Individual status check timeout: {per_status_request_timeout}s, Polling interval: {polling_interval}s, Overall timeout: {overall_timeout}s")

    while True:
        # ... (polling logic for time checks remains the same) ...
        current_loop_time = asyncio.get_event_loop().time()
        elapsed_time = current_loop_time - start_time

        if elapsed_time >= overall_timeout:
            logger.error(f"Overall polling timeout of {overall_timeout}s exceeded for job {req_id}.")
            return {"success": False, "error": "Overall polling timeout exceeded."}

        remaining_time_for_overall_timeout = overall_timeout - elapsed_time
        current_status_check_timeout = min(per_status_request_timeout, remaining_time_for_overall_timeout)

        if current_status_check_timeout <= 0:
             logger.error(f"Not enough time left for another status check for job {req_id} within overall_timeout.")
             return {"success": False, "error": "Not enough time for status check within overall timeout."}

        try:
            async with aiohttp.ClientSession() as session:
                logger.debug(f"Checking status for {req_id} with timeout {current_status_check_timeout}s.")
                async with session.get(
                    f"{status_url}/{req_id}",
                    headers=headers,
                    timeout=current_status_check_timeout
                ) as response:
                    if response.status == 200: # Job completed
                        logger.info(f"AlphaFold2-Multimer job {req_id} completed (status 200).")
                        if response.content_type == 'application/json':
                            try:
                                api_response_json_payload = await response.json()
                                if not isinstance(api_response_json_payload, list):
                                    # Handle case where API might return {"error": ...} directly
                                    if isinstance(api_response_json_payload, dict) and "error" in api_response_json_payload:
                                        logger.error(f"Job {req_id}: API returned error: {api_response_json_payload['error']}")
                                        return {"success": False, "error": api_response_json_payload['error'], "detail": api_response_json_payload.get("detail","")}
                                    logger.error(f"Job {req_id}: Expected API response to be a list of PDB strings, got {type(api_response_json_payload)}.")
                                    return {"success": False, "error": "API response was not a list of PDB strings."}

                                return await _process_pdb_and_scores_from_api( # No output_dir
                                    pdb_contents=api_response_json_payload,
                                    job_id=req_id,
                                    api_response_json=None # Not used currently
                                )
                            except json.JSONDecodeError:
                                logger.error(f"Job {req_id}: Failed to decode JSON response from API.", exc_info=True)
                                raw_text = await response.text()
                                return {"success": False, "error": "Failed to decode JSON response.", "detail": raw_text[:500]}
                        else:
                            raw_text = await response.text()
                            logger.error(f"Job {req_id}: Unexpected content type {response.content_type}. Expected application/json. Response: {raw_text[:500]}")
                            return {"success": False, "error": f"Unexpected content type: {response.content_type}", "detail": raw_text}
                    # ... (rest of polling logic: 202, errors, timeouts remains the same) ...
                    elif response.status == 202:
                        try:
                            job_status_json = await response.json()
                            percent_complete = job_status_json.get('percentComplete', 'N/A')
                            status_message = job_status_json.get('status', 'running')
                            logger.debug(
                                f"Job {req_id} status: {status_message} ({percent_complete}% complete). Polling again in {polling_interval}s."
                            )
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            logger.debug(
                                f"Job {req_id} still running (202 status, non-JSON/malformed JSON body). Polling again in {polling_interval}s."
                            )
                        await asyncio.sleep(polling_interval)
                    else:
                        text = await response.text()
                        logger.error(f"Error checking AlphaFold2-Multimer job status {req_id}: HTTP {response.status} - {text}")
                        return {"success": False, "error": f"Status check failed with HTTP {response.status}", "detail": text}
        except asyncio.TimeoutError:
            logger.warning(f"Client-side timeout ({current_status_check_timeout}s) during status check for job {req_id}. Retrying poll after {polling_interval}s sleep.")
            await asyncio.sleep(polling_interval) # Sleep before next attempt
        except aiohttp.ClientError as e:
            logger.error(f"Client error polling job status for {req_id}: {e}. Retrying poll after {polling_interval}s.", exc_info=True)
            await asyncio.sleep(polling_interval)
        except Exception as e:
            logger.error(f"Unexpected error polling job status {req_id}: {e}", exc_info=True)
            return {"success": False, "error": f"Unexpected polling error: {str(e)}"}