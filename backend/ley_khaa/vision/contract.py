from typing import Literal

from pydantic import BaseModel


class VisionExtraction(BaseModel):
    """What a model returns for one image (spec §3.1).

    Two fields rather than one because the consumers have different budgets:
    the interpreter needs a sentence it can afford inside a prompt, the
    resolver needs the whole CSV as bytes to compute on. Truncating `content`
    to serve the interpreter would hand it half a row of CSV, which is worse
    than a sentence.

    `kind` is a closed Literal on purpose — it decides the checkpoint's file
    extension, so a model answering "png" must fail structured-output
    validation rather than produce an `.png` file full of prose.
    """

    kind: Literal["table", "text"]
    content: str
    summary: str
