/**
 * Length ceilings for the feedback forms (ADR-0005 §6), mirroring the server:
 * `FeedbackRequest.comment` and `SourceRequestBody.note` (`max_length=1000`) and
 * `SourceRequestBody.requested_url` (`max_length=2048`) in
 * `backend/app/api/routes/feedback.py`.
 *
 * Enforced at SUBMIT, never with a `maxLength` attribute (TEST_STRATEGY §8b: the
 * attribute clamps a paste silently). Counted with `questionLength`, the way the
 * server counts. `backend/tests/test_ui_input_limits_match_the_api.py` reads these
 * literals and compares them with the Pydantic fields.
 */
export const MAX_FEEDBACK_COMMENT = 1000;
export const MAX_SOURCE_NOTE = 1000;
export const MAX_SOURCE_URL = 2048;
