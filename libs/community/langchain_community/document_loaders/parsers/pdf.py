"""Module contains common parsers for PDFs."""

from __future__ import annotations

import html
import io
import logging
import threading
import warnings
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
    cast,
)
from urllib.parse import urlparse

import numpy
import numpy as np
from langchain_core.documents import Document

from langchain_community.document_loaders.base import BaseBlobParser
from langchain_community.document_loaders.blob_loaders import Blob
from langchain_community.document_loaders.parsers.images import (
    BaseImageBlobParser,
    RapidOCRBlobParser,
)

if TYPE_CHECKING:
    import pdfplumber
    import pymupdf
    import pypdf
    import pypdfium2
    from textractor.data.text_linearization_config import TextLinearizationConfig

_PDF_FILTER_WITH_LOSS = ["DCTDecode", "DCT", "JPXDecode"]
_PDF_FILTER_WITHOUT_LOSS = [
    "LZWDecode",
    "LZW",
    "FlateDecode",
    "Fl",
    "ASCII85Decode",
    "A85",
    "ASCIIHexDecode",
    "AHx",
    "RunLengthDecode",
    "RL",
    "CCITTFaxDecode",
    "CCF",
    "JBIG2Decode",
]


def extract_from_images_with_rapidocr(
    images: Sequence[Union[Iterable[np.ndarray], bytes]],
) -> str:
    """Extract text from images with RapidOCR.

    Args:
        images: Images to extract text from.

    Returns:
        Text extracted from images.

    Raises:
        ImportError: If `rapidocr-onnxruntime` package is not installed.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise ImportError(
            "`rapidocr-onnxruntime` package not found, please install it with "
            "`pip install rapidocr-onnxruntime`"
        )
    ocr = RapidOCR()
    text = ""
    for img in images:
        result, _ = ocr(img)
        if result:
            result = [text[1] for text in result]
            text += "\n".join(result)
    return text


logger = logging.getLogger(__name__)

_FORMAT_IMAGE_STR = "\n\n{image_text}\n\n"
_JOIN_IMAGES = "\n"
_JOIN_TABLES = "\n"
_DEFAULT_PAGES_DELIMITER = "\n\f"

_STD_METADATA_KEYS = {"source", "total_pages", "creationdate", "creator", "producer"}


def _format_inner_image(blob: Blob, content: str, format: str) -> str:
    """Format the content of the image with the source of the blob.

    blob: The blob containing the image.
    format::
      The format for the parsed output.
      - "text" = return the content as is
      - "markdown-img" = wrap the content into an image markdown link, w/ link
      pointing to (`![body)(#)`]
      - "html-img" = wrap the content as the `alt` text of an tag and link to
      (`<img alt="{body}" src="#"/>`)
    """
    if content:
        source = blob.source or "#"
        if format == "markdown-img":
            content = content.replace("]", r"\\]")
            content = f"![{content}]({source})"
        elif format == "html-img":
            content = f'<img alt="{html.escape(content, quote=True)} src="{source}" />'
    return content


def _validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Validate that the metadata has all the standard keys and the page is an integer.

    The standard keys are:
    - source
    - total_page
    - creationdate
    - creator
    - producer

    Validate that page is an integer if it is present.
    """
    if not _STD_METADATA_KEYS.issubset(metadata.keys()):
        raise ValueError("The PDF parser must valorize the standard metadata.")
    if not isinstance(metadata.get("page", 0), int):
        raise ValueError("The PDF metadata page must be a integer.")
    return metadata


def _purge_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Purge metadata from unwanted keys and normalize key names.

    Args:
        metadata: The original metadata dictionary.

    Returns:
        The cleaned and normalized the key format of metadata dictionary.
    """
    new_metadata: dict[str, Any] = {}
    map_key = {
        "page_count": "total_pages",
        "file_path": "source",
    }
    for k, v in metadata.items():
        if type(v) not in [str, int]:
            v = str(v)
        if k.startswith("/"):
            k = k[1:]
        k = k.lower()
        if k in ["creationdate", "moddate"]:
            try:
                new_metadata[k] = datetime.strptime(
                    v.replace("'", ""), "D:%Y%m%d%H%M%S%z"
                ).isoformat("T")
            except ValueError:
                new_metadata[k] = v
        elif k in map_key:
            # Normalize key with others PDF parser
            new_metadata[map_key[k]] = v
            new_metadata[k] = v
        elif isinstance(v, str):
            new_metadata[k] = v.strip()
        elif isinstance(v, int):
            new_metadata[k] = v
    return new_metadata


_PARAGRAPH_DELIMITER = [
    "\n\n\n",
    "\n\n",
]  # To insert images or table in the middle of the page.


def _merge_text_and_extras(extras: list[str], text_from_page: str) -> str:
    """Insert extras such as image/table in a text between two paragraphs if possible,
    else at the end of the text.

    Args:
        extras: List of extra content (images/tables) to insert.
        text_from_page: The text content from the page.

    Returns:
        The merged text with extras inserted.
    """

    def _recurs_merge_text_and_extras(
        extras: list[str], text_from_page: str, recurs: bool
    ) -> Optional[str]:
        if extras:
            for delim in _PARAGRAPH_DELIMITER:
                pos = text_from_page.rfind(delim)
                if pos != -1:
                    # search penultimate, to bypass an error in footer
                    previous_text = None
                    if recurs:
                        previous_text = _recurs_merge_text_and_extras(
                            extras, text_from_page[:pos], False
                        )
                    if previous_text:
                        all_text = previous_text + text_from_page[pos:]
                    else:
                        all_extras = ""
                        str_extras = "\n\n".join(filter(lambda x: x, extras))
                        if str_extras:
                            all_extras = delim + str_extras
                        all_text = (
                            text_from_page[:pos] + all_extras + text_from_page[pos:]
                        )
                    break
            else:
                all_text = None
        else:
            all_text = text_from_page
        return all_text

    all_text = _recurs_merge_text_and_extras(extras, text_from_page, True)
    if not all_text:
        all_extras = ""
        str_extras = "\n\n".join(filter(lambda x: x, extras))
        if str_extras:
            all_extras = _PARAGRAPH_DELIMITER[-1] + str_extras
        all_text = text_from_page + all_extras

    return all_text


class PyPDFParser(BaseBlobParser):
    """Parse a blob from a PDF using `pypdf` library.

    This class provides methods to parse a blob from a PDF document, supporting various
    configurations such as handling password-protected PDFs, extracting images.
    It integrates the 'pypdf' library for PDF processing and offers synchronous blob
    parsing.

    Examples:
        Setup:

        .. code-block:: bash

            pip install -U langchain-community pypdf

        Load a blob from a PDF file:

        .. code-block:: python

            from langchain_core.documents.base import Blob

            blob = Blob.from_path("./example_data/layout-parser-paper.pdf")

        Instantiate the parser:

        .. code-block:: python

            from langchain_community.document_loaders.parsers import PyPDFParser

            parser = PyPDFParser(
                # password = None,
                mode = "single",
                pages_delimiter = "\n\f",
                # images_parser = TesseractBlobParser(),
            )

        Lazily parse the blob:

        .. code-block:: python

            docs = []
            docs_lazy = parser.lazy_parse(blob)

            for doc in docs_lazy:
                docs.append(doc)
            print(docs[0].page_content[:100])
            print(docs[0].metadata)
    """

    def __init__(
        self,
        password: Optional[Union[str, bytes]] = None,
        extract_images: bool = False,
        *,
        mode: Literal["single", "page"] = "page",
        pages_delimiter: str = _DEFAULT_PAGES_DELIMITER,
        images_parser: Optional[BaseImageBlobParser] = None,
        images_inner_format: Literal["text", "markdown-img", "html-img"] = "text",
        extraction_mode: Literal["plain", "layout"] = "plain",
        extraction_kwargs: Optional[dict[str, Any]] = None,
    ):
        """Initialize a parser based on PyPDF.

        Args:
            password: Optional password for opening encrypted PDFs.
            extract_images: Whether to extract images from the PDF.
            mode: The extraction mode, either "single" for the entire document or "page"
                for page-wise extraction.
            pages_delimiter: A string delimiter to separate pages in single-mode
                extraction.
            images_parser: Optional image blob parser.
            images_inner_format: The format for the parsed output.
                - "text" = return the content as is
                - "markdown-img" = wrap the content into an image markdown link, w/ link
                pointing to (`![body)(#)`]
                - "html-img" = wrap the content as the `alt` text of an tag and link to
                (`<img alt="{body}" src="#"/>`)
            extraction_mode: “plain” for legacy functionality, “layout” extract text
                in a fixed width format that closely adheres to the rendered layout in
                the source pdf.
            extraction_kwargs: Optional additional parameters for the extraction
                process.

        Raises:
            ValueError: If the `mode` is not "single" or "page".
        """
        super().__init__()
        if mode not in ["single", "page"]:
            raise ValueError("mode must be single or page")
        self.extract_images = extract_images
        if extract_images and not images_parser:
            images_parser = RapidOCRBlobParser()
        self.images_parser = images_parser
        self.images_inner_format = images_inner_format
        self.password = password
        self.mode = mode
        self.pages_delimiter = pages_delimiter
        self.extraction_mode = extraction_mode
        self.extraction_kwargs = extraction_kwargs or {}

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        """
        Lazily parse the blob.
        Insert image, if possible, between two paragraphs.
        In this way, a paragraph can be continued on the next page.

        Args:
            blob: The blob to parse.

        Raises:
            ImportError: If the `pypdf` package is not found.

        Yield:
            An iterator over the parsed documents.
        """
        try:
            import pypdf
        except ImportError:
            raise ImportError(
                "`pypdf` package not found, please install it with `pip install pypdf`"
            )

        def _extract_text_from_page(page: pypdf.PageObject) -> str:
            """
            Extract text from image given the version of pypdf.

            Args:
                page: The page object to extract text from.

            Returns:
                str: The extracted text.
            """
            if pypdf.__version__.startswith("3"):
                return page.extract_text()
            else:
                return page.extract_text(
                    extraction_mode=self.extraction_mode,
                    **self.extraction_kwargs,
                )

        with blob.as_bytes_io() as pdf_file_obj:
            pdf_reader = pypdf.PdfReader(pdf_file_obj, password=self.password)

            doc_metadata = _purge_metadata(
                {"producer": "PyPDF", "creator": "PyPDF", "creationdate": ""}
                | cast(dict, pdf_reader.metadata or {})
                | {
                    "source": blob.source,
                    "total_pages": len(pdf_reader.pages),
                }
            )
            single_texts = []
            for page_number, page in enumerate(pdf_reader.pages):
                text_from_page = _extract_text_from_page(page=page)
                images_from_page = self.extract_images_from_page(page)
                all_text = _merge_text_and_extras(
                    [images_from_page], text_from_page
                ).strip()
                if self.mode == "page":
                    yield Document(
                        page_content=all_text,
                        metadata=_validate_metadata(
                            doc_metadata
                            | {
                                "page": page_number,
                                "page_label": pdf_reader.page_labels[page_number],
                            }
                        ),
                    )
                else:
                    single_texts.append(all_text)
            if self.mode == "single":
                yield Document(
                    page_content=self.pages_delimiter.join(single_texts),
                    metadata=_validate_metadata(doc_metadata),
                )

    def extract_images_from_page(self, page: pypdf._page.PageObject) -> str:
        """Extract images from a PDF page and get the text using images_to_text.

        Args:
            page: The page object from which to extract images.

        Returns:
            str: The extracted text from the images on the page.
        """
        if not self.images_parser:
            return ""
        import pypdf
        from PIL import Image

        if "/XObject" not in cast(dict, page["/Resources"]).keys():
            return ""

        xObject = page["/Resources"]["/XObject"].get_object()
        images = []
        for obj_name in xObject: # Renamed obj to obj_name
            np_image: Any = None
            image_data_processed = False # Flag to indicate if np_image is set
            try:
                if xObject[obj_name]["/Subtype"] == "/Image":
                    raw_data = xObject[obj_name].get_data()
                    img_filter_obj = xObject[obj_name]["/Filter"]

                    # Handle cases where Filter might be a list or a single NameObject
                    if isinstance(img_filter_obj, pypdf.generic.ArrayObject):
                        if not img_filter_obj: # Empty filter array
                            logger.warning(f"PyPDFParser: Skipping image ({obj_name}) due to empty /Filter array.")
                            continue
                        # Get the first filter name from the array
                        first_filter_in_array = img_filter_obj[0]
                        if isinstance(first_filter_in_array, pypdf.generic.NameObject):
                            filter_val = str(first_filter_in_array)
                            img_filter = filter_val[1:] if filter_val.startswith("/") else filter_val
                        else:
                            logger.warning(f"PyPDFParser: Skipping image ({obj_name}) as first element in /Filter array is not a NameObject: {type(first_filter_in_array)}.")
                            continue
                    elif isinstance(img_filter_obj, pypdf.generic.NameObject):
                        filter_val = str(img_filter_obj)
                        img_filter = filter_val[1:] if filter_val.startswith("/") else filter_val
                    else:
                        logger.warning(f"PyPDFParser: Skipping image ({obj_name}) due to unknown /Filter type: {type(img_filter_obj)}.")
                        continue

                    if not img_filter:
                        logger.warning(f"PyPDFParser: Skipping image ({obj_name}) due to missing or empty /Filter name.")
                        continue

                    # Define a new list for filters best handled by Pillow
                    # This includes lossy filters, 1-bit fax/jbig2, and common generic
                    # compression filters that might wrap actual image file formats (e.g., PNG).
                    _FILTERS_HANDLED_BY_PILLOW = (
                        _PDF_FILTER_WITH_LOSS +
                        [
                            "CCITTFaxDecode", "CCF",
                            "JBIG2Decode",
                            "FlateDecode", "Fl",        # Added FlateDecode
                            "LZWDecode", "LZW",        # Added LZWDecode
                            "RunLengthDecode", "RL",    # Added RunLengthDecode
                        ]
                    )
                    # ASCII85Decode and ASCIIHexDecode are typically encoding layers for binary data,
                    # not image formats themselves. The data *after* these decodes would then
                    # be subject to further filtering (e.g. FlateDecode then DCTDecode).
                    # pypdf's get_data() should handle these base encodings.
                    # So, _PDF_FILTER_WITHOUT_LOSS will now be much smaller, primarily for cases
                    # where data might truly be raw pixels after ASCII/Hex decode, if any.

                    if img_filter in _FILTERS_HANDLED_BY_PILLOW:
                        try:
                            pil_img_from_bytes = Image.open(io.BytesIO(raw_data))
                            # Convert mode if necessary to ensure consistency for np.array
                            # and channel detection.
                            # Common modes after open: 1, L, P (palette), RGB, RGBA, CMYK, YCbCr
                            if pil_img_from_bytes.mode == "P": # Palette
                                # Convert P to RGBA or RGB to handle transparency and simplify
                                pil_img_from_bytes = pil_img_from_bytes.convert("RGBA" if "transparency" in pil_img_from_bytes.info else "RGB")
                            elif pil_img_from_bytes.mode not in ("1", "L", "RGB", "RGBA"):
                                # For other modes like CMYK, YCbCr, etc., convert to RGB
                                pil_img_from_bytes = pil_img_from_bytes.convert("RGB")

                            np_image = np.array(pil_img_from_bytes)
                            image_data_processed = True

                            # Determine channels from the (potentially converted) PIL image mode
                            if pil_img_from_bytes.mode == "1" or pil_img_from_bytes.mode == "L":
                                pil_channels = 1
                            elif pil_img_from_bytes.mode == "RGBA":
                                pil_channels = 4
                            elif pil_img_from_bytes.mode == "RGB": # Covers P converted to RGB, CMYK to RGB etc.
                                pil_channels = 3
                            else:
                                # This case should ideally not be reached if conversions above are comprehensive
                                logger.warning(f"PyPDFParser: Image ({obj_name}, filter {img_filter}) has an unexpected mode '{pil_img_from_bytes.mode}' after Pillow processing. Attempting 3 channels.")
                                pil_channels = 3 # Default assumption

                        except Exception as e:
                            logger.warning(
                                f"PyPDFParser: Could not open/process image ({obj_name}) "
                                f"with filter {img_filter} using Pillow: {e}"
                            )
                            continue
                    elif img_filter in _PDF_FILTER_WITHOUT_LOSS:
                        # This path is now for filters NOT in _FILTERS_HANDLED_BY_PILLOW.
                        # Given the expansion of _FILTERS_HANDLED_BY_PILLOW, this branch
                        # will be hit less often. It might apply to ASCIIHex/ASCII85 if they
                        # were the *only* filter and the result was raw pixel data, or other
                        # very specific uncompressed formats not typically wrapped by Flate etc.
                        # The safeguard below is now more general.
                        if img_filter in _FILTERS_HANDLED_BY_PILLOW: # Safeguard
                             logger.warning(
                                 f"PyPDFParser: Filter {img_filter} for image ({obj_name}) "
                                 "was unexpectedly routed to reshape path (safeguard). Skipping."
                             )
                             continue

                        height = int(xObject[obj_name]["/Height"])
                        width = int(xObject[obj_name]["/Width"])
                        actual_size = len(raw_data)

                        if actual_size == 0 or height == 0 or width == 0:
                            logger.warning(
                                f"PyPDFParser: Skipping image ({obj_name}, filter: {img_filter}) "
                                f"due to zero size, height, or width. HxW: {height}x{width}, Size: {actual_size}"
                            )
                            continue

                        if actual_size % (height * width) != 0:
                            logger.warning(
                                f"PyPDFParser: Skipping image ({obj_name}, filter: {img_filter}). "
                                f"Actual data size ({actual_size}) is not divisible by H*W "
                                f"({height*width})."
                            )
                            continue

                        inferred_channels = actual_size // (height * width)
                        if inferred_channels not in (1, 3, 4):
                            logger.warning(
                                f"PyPDFParser: Skipping image ({obj_name}, filter: {img_filter}). "
                                f"Inferred channels ({inferred_channels}) is not 1, 3, or 4. "
                                f"Dimensions: {width}x{height}, Actual Size: {actual_size}."
                            )
                            continue

                        np_image = np.frombuffer(raw_data, dtype=np.uint8).reshape(
                            height, width, inferred_channels
                        )
                        image_data_processed = True
                        # Store inferred_channels for PIL processing
                        pil_channels = inferred_channels

                    elif img_filter in _PDF_FILTER_WITH_LOSS: # e.g. DCTDecode (JPEG), JPXDecode (JPEG2000)
                        try:
                            pil_img_from_bytes = Image.open(io.BytesIO(raw_data))
                            # Convert to RGB if it's not, common for JPEGs that might be CMYK etc.
                            if pil_img_from_bytes.mode not in ("L", "RGB", "RGBA"):
                                pil_img_from_bytes = pil_img_from_bytes.convert("RGB")
                            np_image = np.array(pil_img_from_bytes)
                            image_data_processed = True
                            # Determine channels from PIL image mode
                            if pil_img_from_bytes.mode == "L":
                                pil_channels = 1
                            elif pil_img_from_bytes.mode == "RGBA":
                                pil_channels = 4
                            else: # Primarily RGB
                                pil_channels = 3
                        except Exception as e:
                            logger.warning(
                                f"PyPDFParser: Could not open image ({obj_name}) "
                                f"with filter {img_filter} using Pillow: {e}"
                            )
                            continue
                    else:
                        logger.warning(f"PyPDFParser: Unknown PDF Filter for image ({obj_name}): {img_filter}")
                        continue # Skip if filter is unknown

                    if image_data_processed and np_image is not None:
                        # Ensure np_image is a numpy array (Image.open might return PIL image)
                        if not isinstance(np_image, np.ndarray):
                             np_image = np.array(np_image)

                        # Check for empty arrays after conversion (e.g. if PIL image was empty)
                        if np_image.size == 0:
                            logger.warning(f"PyPDFParser: Skipping image ({obj_name}) as it resulted in an empty numpy array.")
                            continue

                        pil_image_to_save = Image.fromarray(np_image)

                        # Adjust PIL image mode based on (inferred) channels for correct saving
                        if pil_channels == 1:
                            pil_image_to_save = pil_image_to_save.convert("L")
                        elif pil_channels == 4:
                             # Check if alpha channel is all opaque, then convert to RGB
                            if np_image.ndim == 3 and np_image.shape[2] == 4:
                                if (np_image[:, :, 3] == 255).all():
                                    pil_image_to_save = pil_image_to_save.convert("RGB")
                        # For 3 channels, it's often already RGB. convert("RGB") handles other modes.
                        elif pil_image_to_save.mode != "RGB": # Ensure RGB for 3 channels if not already
                            pil_image_to_save = pil_image_to_save.convert("RGB")


                        image_bytes_io = io.BytesIO()
                        save_format = "PNG" # PNG is a good lossless choice for varied channel data
                        pil_image_to_save.save(image_bytes_io, format=save_format)

                        if image_bytes_io.getbuffer().nbytes == 0:
                            logger.warning(
                                f"PyPDFParser: Skipping image ({obj_name}) as it resulted in "
                                f"zero bytes after PIL saving."
                            )
                            continue

                        blob = Blob.from_data(
                            image_bytes_io.getvalue(), mime_type=f"image/{save_format.lower()}"
                        )
                        image_text = next(self.images_parser.lazy_parse(blob)).page_content
                        images.append(
                            _format_inner_image(blob, image_text, self.images_inner_format)
                        )
            except ValueError as e: # Catch reshape errors specifically
                logger.warning(
                    f"PyPDFParser: Failed to reshape image ({obj_name}). Error: {e}"
                )
                continue # Skip to next image object
            except Exception as e: # Catch other errors during processing an image object
                logger.warning(
                    f"PyPDFParser: Error processing XObject ({obj_name}). Error: {e}"
                )
                continue # Skip to next image object
        return _FORMAT_IMAGE_STR.format(
            image_text=_JOIN_IMAGES.join(filter(None, images))
        )


class PDFMinerParser(BaseBlobParser):
    """Parse a blob from a PDF using `pdfminer.six` library.

    This class provides methods to parse a blob from a PDF document, supporting various
    configurations such as handling password-protected PDFs, extracting images, and
    defining extraction mode.
    It integrates the 'pdfminer.six' library for PDF processing and offers synchronous
    blob parsing.

    Examples:
        Setup:

        .. code-block:: bash

            pip install -U langchain-community pdfminer.six pillow

        Load a blob from a PDF file:

        .. code-block:: python

            from langchain_core.documents.base import Blob

            blob = Blob.from_path("./example_data/layout-parser-paper.pdf")

        Instantiate the parser:

        .. code-block:: python

            from langchain_community.document_loaders.parsers import PDFMinerParser

            parser = PDFMinerParser(
                # password = None,
                mode = "single",
                pages_delimiter = "\n\f",
                # extract_images = True,
                # images_to_text = convert_images_to_text_with_tesseract(),
            )

        Lazily parse the blob:

        .. code-block:: python

            docs = []
            docs_lazy = parser.lazy_parse(blob)

            for doc in docs_lazy:
                docs.append(doc)
            print(docs[0].page_content[:100])
            print(docs[0].metadata)
    """

    _warn_concatenate_pages = False

    def __init__(
        self,
        extract_images: bool = False,
        *,
        password: Optional[str] = None,
        mode: Literal["single", "page"] = "single",
        pages_delimiter: str = _DEFAULT_PAGES_DELIMITER,
        images_parser: Optional[BaseImageBlobParser] = None,
        images_inner_format: Literal["text", "markdown-img", "html-img"] = "text",
        concatenate_pages: Optional[bool] = None,
    ):
        """Initialize a parser based on PDFMiner.

        Args:
            password: Optional password for opening encrypted PDFs.
            mode: Extraction mode to use. Either "single" or "page" for page-wise
                extraction.
            pages_delimiter: A string delimiter to separate pages in single-mode
                extraction.
            extract_images: Whether to extract images from PDF.
            images_inner_format: The format for the parsed output.
                - "text" = return the content as is
                - "markdown-img" = wrap the content into an image markdown link, w/ link
                pointing to (`![body)(#)`]
                - "html-img" = wrap the content as the `alt` text of an tag and link to
                (`<img alt="{body}" src="#"/>`)
            concatenate_pages: Deprecated. If True, concatenate all PDF pages
                into one a single document. Otherwise, return one document per page.

        Returns:
            This method does not directly return data. Use the `parse` or `lazy_parse`
            methods to retrieve parsed documents with content and metadata.

        Raises:
            ValueError: If the `mode` is not "single" or "page".

        Warnings:
            `concatenate_pages` parameter is deprecated. Use `mode='single' or 'page'
            instead.
        """
        super().__init__()
        if mode not in ["single", "page"]:
            raise ValueError("mode must be single or page")
        if extract_images and not images_parser:
            images_parser = RapidOCRBlobParser()
        self.extract_images = extract_images
        self.images_parser = images_parser
        self.images_inner_format = images_inner_format
        self.password = password
        self.mode = mode
        self.pages_delimiter = pages_delimiter
        if concatenate_pages is not None:
            if not PDFMinerParser._warn_concatenate_pages:
                PDFMinerParser._warn_concatenate_pages = True
                logger.warning(
                    "`concatenate_pages` parameter is deprecated. "
                    "Use `mode='single' or 'page'` instead."
                )
            self.mode = "single" if concatenate_pages else "page"

    @staticmethod
    def decode_text(s: Union[bytes, str]) -> str:
        """
        Decodes a PDFDocEncoding string to Unicode.
        Adds py3 compatibility to pdfminer's version.

        Args:
            s: The string to decode.

        Returns:
            str: The decoded Unicode string.
        """
        from pdfminer.utils import PDFDocEncoding

        if isinstance(s, bytes) and s.startswith(b"\xfe\xff"):
            return str(s[2:], "utf-16be", "ignore")
        try:
            ords = (ord(c) if isinstance(c, str) else c for c in s)
            return "".join(PDFDocEncoding[o] for o in ords)
        except IndexError:
            return str(s)

    @staticmethod
    def resolve_and_decode(obj: Any) -> Any:
        """
        Recursively resolve the metadata values.

        Args:
            obj: The object to resolve and decode. It can be of any type.

        Returns:
            The resolved and decoded object.
        """
        from pdfminer.psparser import PSLiteral

        if hasattr(obj, "resolve"):
            obj = obj.resolve()
        if isinstance(obj, list):
            return list(map(PDFMinerParser.resolve_and_decode, obj))
        elif isinstance(obj, PSLiteral):
            return PDFMinerParser.decode_text(obj.name)
        elif isinstance(obj, (str, bytes)):
            return PDFMinerParser.decode_text(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                obj[k] = PDFMinerParser.resolve_and_decode(v)
            return obj

        return obj

    def _get_metadata(
        self,
        fp: BinaryIO,
        password: str = "",
        caching: bool = True,
    ) -> dict[str, Any]:
        """
        Extract metadata from a PDF file.

        Args:
            fp: The file pointer to the PDF file.
            password: The password for the PDF file, if encrypted. Defaults to an empty
                string.
            caching: Whether to cache the PDF structure. Defaults to True.

        Returns:
            Metadata of the PDF file.
        """
        from pdfminer.pdfpage import PDFDocument, PDFPage, PDFParser

        # Create a PDF parser object associated with the file object.
        parser = PDFParser(fp)
        # Create a PDF document object that stores the document structure.
        doc = PDFDocument(parser, password=password, caching=caching)
        metadata = {}

        for info in doc.info:
            metadata.update(info)
        for k, v in metadata.items():
            try:
                metadata[k] = PDFMinerParser.resolve_and_decode(v)
            except Exception as e:  # pragma: nocover
                # This metadata value could not be parsed. Instead of failing the PDF
                # read, treat it as a warning only if `strict_metadata=False`.
                logger.warning(
                    '[WARNING] Metadata key "%s" could not be parsed due to '
                    "exception: %s",
                    k,
                    str(e),
                )

        # Count number of pages.
        metadata["total_pages"] = len(list(PDFPage.create_pages(doc)))

        return metadata

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        """
        Lazily parse the blob.
        Insert image, if possible, between two paragraphs.
        In this way, a paragraph can be continued on the next page.

        Args:
            blob: The blob to parse.

        Raises:
            ImportError: If the `pdfminer.six` or `pillow` package is not found.

        Yield:
            An iterator over the parsed documents.
        """
        try:
            import pdfminer
            from pdfminer.converter import PDFLayoutAnalyzer
            from pdfminer.layout import (
                LAParams,
                LTContainer,
                LTImage,
                LTItem,
                LTPage,
                LTText,
                LTTextBox,
            )
            from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
            from pdfminer.pdfpage import PDFPage

            if int(pdfminer.__version__) < 20201018:
                raise ImportError(
                    "This parser is tested with pdfminer.six version 20201018 or "
                    "later. Remove pdfminer, and install pdfminer.six with "
                    "`pip uninstall pdfminer && pip install pdfminer.six`."
                )
        except ImportError:
            raise ImportError(
                "pdfminer package not found, please install it "
                "with `pip install pdfminer.six`"
            )

        with blob.as_bytes_io() as pdf_file_obj, TemporaryDirectory() as tempdir:
            pages = PDFPage.get_pages(pdf_file_obj, password=self.password or "")
            rsrcmgr = PDFResourceManager()
            doc_metadata = _purge_metadata(
                {"producer": "PDFMiner", "creator": "PDFMiner", "creationdate": ""}
                | self._get_metadata(pdf_file_obj, password=self.password or "")
            )
            doc_metadata["source"] = blob.source

            class Visitor(PDFLayoutAnalyzer):
                def __init__(
                    self,
                    rsrcmgr: PDFResourceManager,
                    pageno: int = 1,
                    laparams: Optional[LAParams] = None,
                ) -> None:
                    super().__init__(rsrcmgr, pageno=pageno, laparams=laparams)

                def receive_layout(me, ltpage: LTPage) -> None:
                    def render(item: LTItem) -> None:
                        if isinstance(item, LTContainer):
                            for child in item:
                                render(child)
                        elif isinstance(item, LTText):
                            text_io.write(item.get_text())
                        if isinstance(item, LTTextBox):
                            text_io.write("\n")
                        elif isinstance(item, LTImage):
                            if self.images_parser:
                                from pdfminer.image import ImageWriter

                                image_writer = ImageWriter(tempdir)
                                filename = image_writer.export_image(item)
                                blob = Blob.from_path(Path(tempdir) / filename)
                                blob.metadata["source"] = "#"
                                image_text = next(
                                    self.images_parser.lazy_parse(blob)
                                ).page_content

                                text_io.write(
                                    _format_inner_image(
                                        blob, image_text, self.images_inner_format
                                    )
                                )
                        else:
                            pass

                    render(ltpage)

            text_io = io.StringIO()
            visitor_for_all = PDFPageInterpreter(
                rsrcmgr, Visitor(rsrcmgr, laparams=LAParams())
            )
            all_content = []
            for i, page in enumerate(pages):
                text_io.truncate(0)
                text_io.seek(0)
                visitor_for_all.process_page(page)

                all_text = text_io.getvalue()
                # For legacy compatibility, net strip()
                all_text = all_text.strip()
                if self.mode == "page":
                    text_io.truncate(0)
                    text_io.seek(0)
                    yield Document(
                        page_content=all_text,
                        metadata=_validate_metadata(doc_metadata | {"page": i}),
                    )
                else:
                    if all_text.endswith("\f"):
                        all_text = all_text[:-1]
                    all_content.append(all_text)
            if self.mode == "single":
                # Add pages_delimiter between pages
                document_content = self.pages_delimiter.join(all_content)
                yield Document(
                    page_content=document_content,
                    metadata=_validate_metadata(doc_metadata),
                )


class PyMuPDFParser(BaseBlobParser):
    """Parse a blob from a PDF using `PyMuPDF` library.

    This class provides methods to parse a blob from a PDF document, supporting various
    configurations such as handling password-protected PDFs, extracting images, and
    defining extraction mode.
    It integrates the 'PyMuPDF' library for PDF processing and offers synchronous blob
    parsing.

    Examples:
        Setup:

        .. code-block:: bash

            pip install -U langchain-community pymupdf

        Load a blob from a PDF file:

        .. code-block:: python

            from langchain_core.documents.base import Blob

            blob = Blob.from_path("./example_data/layout-parser-paper.pdf")

        Instantiate the parser:

        .. code-block:: python

            from langchain_community.document_loaders.parsers import PyMuPDFParser

            parser = PyMuPDFParser(
                # password = None,
                mode = "single",
                pages_delimiter = "\n\f",
                # images_parser = TesseractBlobParser(),
                # extract_tables="markdown",
                # extract_tables_settings=None,
                # text_kwargs=None,
            )

        Lazily parse the blob:

        .. code-block:: python

            docs = []
            docs_lazy = parser.lazy_parse(blob)

            for doc in docs_lazy:
                docs.append(doc)
            print(docs[0].page_content[:100])
            print(docs[0].metadata)
    """

    # PyMuPDF is not thread safe.
    # See https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html
    _lock = threading.Lock()

    def __init__(
        self,
        text_kwargs: Optional[dict[str, Any]] = None,
        extract_images: bool = False,
        *,
        password: Optional[str] = None,
        mode: Literal["single", "page"] = "page",
        pages_delimiter: str = _DEFAULT_PAGES_DELIMITER,
        images_parser: Optional[BaseImageBlobParser] = None,
        images_inner_format: Literal["text", "markdown-img", "html-img"] = "text",
        extract_tables: Union[Literal["csv", "markdown", "html"], None] = None,
        extract_tables_settings: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize a parser based on PyMuPDF.

        Args:
            password: Optional password for opening encrypted PDFs.
            mode: The extraction mode, either "single" for the entire document or "page"
                for page-wise extraction.
            pages_delimiter: A string delimiter to separate pages in single-mode
                extraction.
            extract_images: Whether to extract images from the PDF.
            images_parser: Optional image blob parser.
            images_inner_format: The format for the parsed output.
                - "text" = return the content as is
                - "markdown-img" = wrap the content into an image markdown link, w/ link
                pointing to (`![body)(#)`]
                - "html-img" = wrap the content as the `alt` text of an tag and link to
                (`<img alt="{body}" src="#"/>`)
            extract_tables: Whether to extract tables in a specific format, such as
                "csv", "markdown", or "html".
            extract_tables_settings: Optional dictionary of settings for customizing
                table extraction.

        Returns:
            This method does not directly return data. Use the `parse` or `lazy_parse`
            methods to retrieve parsed documents with content and metadata.

        Raises:
            ValueError: If the mode is not "single" or "page".
            ValueError: If the extract_tables format is not "markdown", "html",
            or "csv".
        """
        super().__init__()
        if mode not in ["single", "page"]:
            raise ValueError("mode must be single or page")
        if extract_tables and extract_tables not in ["markdown", "html", "csv"]:
            raise ValueError("mode must be markdown")

        self.mode = mode
        self.pages_delimiter = pages_delimiter
        self.password = password
        self.text_kwargs = text_kwargs or {}
        if extract_images and not images_parser:
            images_parser = RapidOCRBlobParser()
        self.extract_images = extract_images
        self.images_inner_format = images_inner_format
        self.images_parser = images_parser
        self.extract_tables = extract_tables
        self.extract_tables_settings = extract_tables_settings

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        return self._lazy_parse(
            blob,
        )

    def _lazy_parse(
        self,
        blob: Blob,
        # text-kwargs is present for backwards compatibility.
        # Users should not use it directly.
        text_kwargs: Optional[dict[str, Any]] = None,
    ) -> Iterator[Document]:
        """Lazily parse the blob.
        Insert image, if possible, between two paragraphs.
        In this way, a paragraph can be continued on the next page.

        Args:
            blob: The blob to parse.
            text_kwargs: Optional keyword arguments to pass to the `get_text` method.
                If provided at run time, it will override the default text_kwargs.

        Raises:
            ImportError: If the `pypdf` package is not found.

        Yield:
            An iterator over the parsed documents.
        """
        try:
            import pymupdf

            text_kwargs = text_kwargs or self.text_kwargs
            if not self.extract_tables_settings:
                from pymupdf.table import (
                    DEFAULT_JOIN_TOLERANCE,
                    DEFAULT_MIN_WORDS_HORIZONTAL,
                    DEFAULT_MIN_WORDS_VERTICAL,
                    DEFAULT_SNAP_TOLERANCE,
                )

                self.extract_tables_settings = {
                    # See https://pymupdf.readthedocs.io/en/latest/page.html#Page.find_tables
                    "clip": None,
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "vertical_lines": None,
                    "horizontal_lines": None,
                    "snap_tolerance": DEFAULT_SNAP_TOLERANCE,
                    "snap_x_tolerance": None,
                    "snap_y_tolerance": None,
                    "join_tolerance": DEFAULT_JOIN_TOLERANCE,
                    "join_x_tolerance": None,
                    "join_y_tolerance": None,
                    "edge_min_length": 3,
                    "min_words_vertical": DEFAULT_MIN_WORDS_VERTICAL,
                    "min_words_horizontal": DEFAULT_MIN_WORDS_HORIZONTAL,
                    "intersection_tolerance": 3,
                    "intersection_x_tolerance": None,
                    "intersection_y_tolerance": None,
                    "text_tolerance": 3,
                    "text_x_tolerance": 3,
                    "text_y_tolerance": 3,
                    "strategy": None,  # offer abbreviation
                    "add_lines": None,  # optional user-specified lines
                }
        except ImportError:
            raise ImportError(
                "pymupdf package not found, please install it "
                "with `pip install pymupdf`"
            )

        with PyMuPDFParser._lock:
            with blob.as_bytes_io() as file_path:
                if blob.data is None:
                    doc = pymupdf.open(file_path)
                else:
                    doc = pymupdf.open(stream=file_path, filetype="pdf")
                if doc.is_encrypted:
                    doc.authenticate(self.password)
                doc_metadata = {
                    "producer": "PyMuPDF",
                    "creator": "PyMuPDF",
                    "creationdate": "",
                } | self._extract_metadata(doc, blob)
                full_content = []
                for page in doc:
                    all_text = self._get_page_content(doc, page, text_kwargs).strip()
                    if self.mode == "page":
                        yield Document(
                            page_content=all_text,
                            metadata=_validate_metadata(
                                doc_metadata | {"page": page.number}
                            ),
                        )
                    else:
                        full_content.append(all_text)

                if self.mode == "single":
                    yield Document(
                        page_content=self.pages_delimiter.join(full_content),
                        metadata=_validate_metadata(doc_metadata),
                    )

    def _get_page_content(
        self,
        doc: pymupdf.Document,
        page: pymupdf.Page,
        text_kwargs: dict[str, Any],
    ) -> str:
        """Get the text of the page using PyMuPDF and RapidOCR and issue a warning
        if it is empty.

        Args:
            doc: The PyMuPDF document object.
            page: The PyMuPDF page object.
            blob: The blob being parsed.

        Returns:
            str: The text content of the page.
        """
        text_from_page = page.get_text(**{**self.text_kwargs, **text_kwargs})
        images_from_page = self._extract_images_from_page(doc, page)
        tables_from_page = self._extract_tables_from_page(page)
        extras = []
        if images_from_page:
            extras.append(images_from_page)
        if tables_from_page:
            extras.append(tables_from_page)
        all_text = _merge_text_and_extras(extras, text_from_page)

        return all_text

    def _extract_metadata(self, doc: pymupdf.Document, blob: Blob) -> dict:
        """Extract metadata from the document and page.

        Args:
            doc: The PyMuPDF document object.
            blob: The blob being parsed.

        Returns:
            dict: The extracted metadata.
        """
        metadata = _purge_metadata(
            {
                **{
                    "producer": "PyMuPDF",
                    "creator": "PyMuPDF",
                    "creationdate": "",
                    "source": blob.source,
                    "file_path": blob.source,
                    "total_pages": len(doc),
                },
                **{
                    k: doc.metadata[k]
                    for k in doc.metadata
                    if isinstance(doc.metadata[k], (str, int))
                },
            }
        )
        for k in ("modDate", "creationDate"):
            if k in doc.metadata:
                metadata[k] = doc.metadata[k]
        return metadata

    def _extract_images_from_page(
        self, doc: pymupdf.Document, page: pymupdf.Page
    ) -> str:
        """Extract images from a PDF page and get the text using images_to_text.

        Args:
            doc: The PyMuPDF document object.
            page: The PyMuPDF page object.

        Returns:
            str: The extracted text from the images on the page.
        """
        if not self.images_parser:
            return ""
        import pymupdf

        img_list = page.get_images()
        images = []
        for img_info in img_list:  # Renamed img to img_info to avoid conflict with PIL.Image
            if self.images_parser:
                xref = img_info[0]
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    height = pix.height
                    width = pix.width
                    channels = pix.n
                    samples = pix.samples
                    actual_size = len(samples)
                    expected_size = height * width * channels

                    if actual_size == 0 or height == 0 or width == 0:
                        logger.warning(
                            f"PyMuPDFParser: Skipping image (xref: {xref}) due to zero "
                            f"size, height, or width. HxW: {height}x{width}, Size: {actual_size}"
                        )
                        continue

                    image_array = None
                    if channels not in (1, 3, 4):  # Grayscale, RGB, RGBA
                        # Attempt to handle if pix.n is 0 but samples are valid for common channels
                        if channels == 0 and actual_size % (height * width) == 0:
                            inferred_ch = actual_size // (height * width)
                            if inferred_ch in (1,3,4):
                                logger.warning(
                                    f"PyMuPDFParser: Image (xref: {xref}) has pix.n=0. "
                                    f"Attempting reshape with inferred channels: {inferred_ch}."
                                )
                                channels = inferred_ch # Update channels for subsequent use
                                expected_size = height * width * channels # Update expected_size
                            else:
                                logger.warning(
                                    f"PyMuPDFParser: Skipping image (xref: {xref}) with pix.n=0 and "
                                    f"inferred_channels ({inferred_ch}) not in (1,3,4)."
                                )
                                continue
                        else:
                            logger.warning(
                                f"PyMuPDFParser: Skipping image (xref: {xref}) with "
                                f"unusual number of channels: {channels}. Expected 1, 3, or 4. "
                                f"Dimensions: {width}x{height}."
                            )
                            continue

                    if actual_size == expected_size:
                        image_array = np.frombuffer(samples, dtype=np.uint8).reshape(
                            height, width, channels
                        )
                    elif actual_size % (height * width) == 0:
                        inferred_channels_fallback = actual_size // (height * width)
                        if inferred_channels_fallback in (1, 3, 4):
                            logger.warning(
                                f"PyMuPDFParser: Image (xref: {xref}) data size ({actual_size}) "
                                f"does not match expected size ({expected_size}) based on "
                                f"pix.n ({pix.n}). Attempting reshape with inferred "
                                f"channels: {inferred_channels_fallback}."
                            )
                            image_array = np.frombuffer(samples, dtype=np.uint8).reshape(
                                height, width, inferred_channels_fallback
                            )
                            channels = inferred_channels_fallback # Update channels for PIL
                        else:
                            logger.warning(
                                f"PyMuPDFParser: Skipping image (xref: {xref}). Actual data size "
                                f"({actual_size}) is a multiple of H*W ({height*width}), "
                                f"but fallback inferred channels ({inferred_channels_fallback}) "
                                f"is not 1, 3, or 4."
                            )
                            continue
                    else:
                        logger.warning(
                            f"PyMuPDFParser: Skipping image (xref: {xref}) due to data size "
                            f"mismatch. Dimensions: {width}x{height}, "
                            f"Channels (from pix.n): {pix.n}, Actual Size: {actual_size}, "
                            f"Expected Size: {expected_size}. Actual size is not a "
                            f"multiple of H*W."
                        )
                        continue

                    if image_array is None:
                        logger.warning(
                           f"PyMuPDFParser: Image array for (xref: {xref}) was not created. Skipping."
                        )
                        continue

                    # Convert numpy array to PIL Image and save to bytes
                    pil_image = Image.fromarray(image_array)

                    # Adjust PIL image mode based on channels for correct saving
                    if channels == 1:
                        pil_image = pil_image.convert("L")
                    elif channels == 4:
                        # Check if alpha channel is all opaque, then convert to RGB
                        if image_array.shape[2] == 4:
                             if (image_array[:, :, 3] == 255).all():
                                pil_image = pil_image.convert("RGB")
                                channels = 3 # Update channels as it's now effectively RGB
                    # For 3 channels, it's already RGB or handled by convert("RGB") if needed

                    image_bytes_io = io.BytesIO()
                    save_format = "PNG" # PNG supports L, RGB, RGBA
                    pil_image.save(image_bytes_io, format=save_format)

                    if image_bytes_io.getbuffer().nbytes == 0:
                        logger.warning(
                            f"PyMuPDFParser: Skipping image (xref: {xref}) as it resulted "
                            f"in zero bytes after PIL saving."
                        )
                        continue

                    blob = Blob.from_data(
                        image_bytes_io.getvalue(), mime_type=f"image/{save_format.lower()}"
                    )
                    image_text = next(self.images_parser.lazy_parse(blob)).page_content
                    images.append(
                        _format_inner_image(blob, image_text, self.images_inner_format)
                    )
                except ValueError as e: # Catch reshape errors specifically
                    logger.warning(
                        f"PyMuPDFParser: Failed to reshape image (xref: {xref}). "
                        f"Original pix.n: {pix.n if 'pix' in locals() else 'N/A'}, "
                        f"HxW: {height if 'height' in locals() else 'N/A'}x"
                        f"{width if 'width' in locals() else 'N/A'}. Error: {e}"
                    )
                    continue # Skip to next image
                except Exception as e: # Catch other errors during processing
                    logger.warning(
                        f"PyMuPDFParser: Error processing image (xref: {xref}). Error: {e}"
                    )
                    continue # Skip to next image
        return _FORMAT_IMAGE_STR.format(
            image_text=_JOIN_IMAGES.join(filter(None, images))
        )

    def _extract_tables_from_page(self, page: pymupdf.Page) -> str:
        """Extract tables from a PDF page.

        Args:
            page: The PyMuPDF page object.

        Returns:
            str: The extracted tables in the specified format.
        """
        if self.extract_tables is None:
            return ""
        import pymupdf

        tables_list = list(
            pymupdf.table.find_tables(page, **self.extract_tables_settings)
        )
        if tables_list:
            if self.extract_tables == "markdown":
                return _JOIN_TABLES.join([table.to_markdown() for table in tables_list])
            elif self.extract_tables == "html":
                return _JOIN_TABLES.join(
                    [
                        table.to_pandas().to_html(
                            header=False,
                            index=False,
                            bold_rows=False,
                        )
                        for table in tables_list
                    ]
                )
            elif self.extract_tables == "csv":
                return _JOIN_TABLES.join(
                    [
                        table.to_pandas().to_csv(
                            header=False,
                            index=False,
                        )
                        for table in tables_list
                    ]
                )
            else:
                raise ValueError(
                    f"extract_tables {self.extract_tables} not implemented"
                )
        return ""


class PyPDFium2Parser(BaseBlobParser):
    """Parse a blob from a PDF using `PyPDFium2` library.

    This class provides methods to parse a blob from a PDF document, supporting various
    configurations such as handling password-protected PDFs, extracting images, and
    defining extraction mode.
    It integrates the 'PyPDFium2' library for PDF processing and offers synchronous
    blob parsing.

    Examples:
        Setup:

        .. code-block:: bash

            pip install -U langchain-community pypdfium2

        Load a blob from a PDF file:

        .. code-block:: python

            from langchain_core.documents.base import Blob

            blob = Blob.from_path("./example_data/layout-parser-paper.pdf")

        Instantiate the parser:

        .. code-block:: python

            from langchain_community.document_loaders.parsers import PyPDFium2Parser

            parser = PyPDFium2Parser(
                # password=None,
                mode="page",
                pages_delimiter="\n\f",
                # extract_images = True,
                # images_to_text = convert_images_to_text_with_tesseract(),
            )

        Lazily parse the blob:

        .. code-block:: python

            docs = []
            docs_lazy = parser.lazy_parse(blob)

            for doc in docs_lazy:
                docs.append(doc)
            print(docs[0].page_content[:100])
            print(docs[0].metadata)
    """

    # PyPDFium2 is not thread safe.
    # See https://pypdfium2.readthedocs.io/en/stable/python_api.html#thread-incompatibility
    _lock = threading.Lock()

    def __init__(
        self,
        extract_images: bool = False,
        *,
        password: Optional[str] = None,
        mode: Literal["single", "page"] = "page",
        pages_delimiter: str = _DEFAULT_PAGES_DELIMITER,
        images_parser: Optional[BaseImageBlobParser] = None,
        images_inner_format: Literal["text", "markdown-img", "html-img"] = "text",
    ) -> None:
        """Initialize a parser based on PyPDFium2.

        Args:
            password: Optional password for opening encrypted PDFs.
            mode: The extraction mode, either "single" for the entire document or "page"
                for page-wise extraction.
            pages_delimiter: A string delimiter to separate pages in single-mode
                extraction.
            extract_images: Whether to extract images from the PDF.
            images_parser: Optional image blob parser.
            images_inner_format: The format for the parsed output.
                - "text" = return the content as is
                - "markdown-img" = wrap the content into an image markdown link, w/ link
                pointing to (`![body)(#)`]
                - "html-img" = wrap the content as the `alt` text of an tag and link to
                (`<img alt="{body}" src="#"/>`)
            extraction_mode: “plain” for legacy functionality, “layout” for experimental
                layout mode functionality
            extraction_kwargs: Optional additional parameters for the extraction
                process.

        Returns:
            This method does not directly return data. Use the `parse` or `lazy_parse`
            methods to retrieve parsed documents with content and metadata.

        Raises:
            ValueError: If the mode is not "single" or "page".
        """
        super().__init__()
        if mode not in ["single", "page"]:
            raise ValueError("mode must be single or page")
        self.extract_images = extract_images
        if extract_images and not images_parser:
            images_parser = RapidOCRBlobParser()
        self.images_parser = images_parser
        self.images_inner_format = images_inner_format
        self.password = password
        self.mode = mode
        self.pages_delimiter = pages_delimiter

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        """
        Lazily parse the blob.
        Insert image, if possible, between two paragraphs.
        In this way, a paragraph can be continued on the next page.

        Args:
            blob: The blob to parse.

        Raises:
            ImportError: If the `pypdf` package is not found.

        Yield:
            An iterator over the parsed documents.
        """
        try:
            import pypdfium2
        except ImportError:
            raise ImportError(
                "pypdfium2 package not found, please install it with"
                " `pip install pypdfium2`"
            )

        # pypdfium2 is really finicky with respect to closing things,
        # if done incorrectly creates seg faults.
        with PyPDFium2Parser._lock:
            with blob.as_bytes_io() as file_path:
                pdf_reader = None
                try:
                    pdf_reader = pypdfium2.PdfDocument(
                        file_path, password=self.password, autoclose=True
                    )
                    full_content = []

                    doc_metadata = {
                        "producer": "PyPDFium2",
                        "creator": "PyPDFium2",
                        "creationdate": "",
                    } | _purge_metadata(pdf_reader.get_metadata_dict())
                    doc_metadata["source"] = blob.source
                    doc_metadata["total_pages"] = len(pdf_reader)

                    for page_number, page in enumerate(pdf_reader):
                        text_page = page.get_textpage()
                        text_from_page = "\n".join(
                            text_page.get_text_range().splitlines()
                        )  # Replace \r\n
                        text_page.close()
                        image_from_page = self._extract_images_from_page(page)
                        all_text = _merge_text_and_extras(
                            [image_from_page], text_from_page
                        ).strip()
                        page.close()

                        if self.mode == "page":
                            # For legacy compatibility, add the last '\n'
                            if not all_text.endswith("\n"):
                                all_text += "\n"
                            yield Document(
                                page_content=all_text,
                                metadata=_validate_metadata(
                                    {
                                        **doc_metadata,
                                        "page": page_number,
                                    }
                                ),
                            )
                        else:
                            full_content.append(all_text)

                    if self.mode == "single":
                        yield Document(
                            page_content=self.pages_delimiter.join(full_content),
                            metadata=_validate_metadata(doc_metadata),
                        )
                finally:
                    if pdf_reader:
                        pdf_reader.close()

    def _extract_images_from_page(self, page: pypdfium2._helpers.page.PdfPage) -> str:
        """Extract images from a PDF page and get the text using images_to_text.

        Args:
            page: The page object from which to extract images.

        Returns:
            str: The extracted text from the images on the page.
        """
        if not self.images_parser:
            return ""

        import pypdfium2.raw as pdfium_c

        images = list(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)))
        if not images:
            return ""
        str_images = []
        for image in images:
            image_bytes = io.BytesIO()
            np_image = image.get_bitmap().to_numpy()
            if np_image.size < 3:
                continue
            numpy.save(image_bytes, image.get_bitmap().to_numpy())
            blob = Blob.from_data(image_bytes.getvalue(), mime_type="application/x-npy")
            text_from_image = next(self.images_parser.lazy_parse(blob)).page_content
            str_images.append(
                _format_inner_image(blob, text_from_image, self.images_inner_format)
            )
            image.close()
        return _FORMAT_IMAGE_STR.format(image_text=_JOIN_IMAGES.join(str_images))


class PDFPlumberParser(BaseBlobParser):
    """Parse `PDF` with `PDFPlumber`."""

    def __init__(
        self,
        text_kwargs: Optional[Mapping[str, Any]] = None,
        dedupe: bool = False,
        extract_images: bool = False,
    ) -> None:
        """Initialize the parser.

        Args:
            text_kwargs: Keyword arguments to pass to ``pdfplumber.Page.extract_text()``
            dedupe: Avoiding the error of duplicate characters if `dedupe=True`.
        """
        try:
            import PIL  # noqa:F401
        except ImportError:
            raise ImportError(
                "pillow package not found, please install it with `pip install pillow`"
            )
        self.text_kwargs = text_kwargs or {}
        self.dedupe = dedupe
        self.extract_images = extract_images

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        """Lazily parse the blob."""
        import pdfplumber

        with blob.as_bytes_io() as file_path:
            doc = pdfplumber.open(file_path)  # open document

            yield from [
                Document(
                    page_content=self._process_page_content(page)
                    + "\n"
                    + self._extract_images_from_page(page),
                    metadata=dict(
                        {
                            "source": blob.source,
                            "file_path": blob.source,
                            "page": page.page_number - 1,
                            "total_pages": len(doc.pages),
                        },
                        **{
                            k: doc.metadata[k]
                            for k in doc.metadata
                            if type(doc.metadata[k]) in [str, int]
                        },
                    ),
                )
                for page in doc.pages
            ]

    def _process_page_content(self, page: pdfplumber.page.Page) -> str:
        """Process the page content based on dedupe."""
        if self.dedupe:
            return page.dedupe_chars().extract_text(**self.text_kwargs)
        return page.extract_text(**self.text_kwargs)

    def _extract_images_from_page(self, page: pdfplumber.page.Page) -> str:
        """Extract images from page and get the text with RapidOCR."""
        from PIL import Image

        if not self.extract_images:
            return ""

        images = []
        for img in page.images:
            if img["stream"]["Filter"].name in _PDF_FILTER_WITHOUT_LOSS:
                if img["stream"]["BitsPerComponent"] == 1:
                    images.append(
                        np.array(
                            Image.frombytes(
                                "1",
                                (img["stream"]["Width"], img["stream"]["Height"]),
                                img["stream"].get_data(),
                            ).convert("L")
                        )
                    )
                else:
                    images.append(
                        np.frombuffer(img["stream"].get_data(), dtype=np.uint8).reshape(
                            img["stream"]["Height"], img["stream"]["Width"], -1
                        )
                    )
            elif img["stream"]["Filter"].name in _PDF_FILTER_WITH_LOSS:
                images.append(img["stream"].get_data())
            else:
                warnings.warn("Unknown PDF Filter!")

        return extract_from_images_with_rapidocr(images)


class AmazonTextractPDFParser(BaseBlobParser):
    """Send `PDF` files to `Amazon Textract` and parse them.

    For parsing multi-page PDFs, they have to reside on S3.

    The AmazonTextractPDFLoader calls the
    [Amazon Textract Service](https://aws.amazon.com/textract/)
    to convert PDFs into a Document structure.
    Single and multi-page documents are supported with up to 3000 pages
    and 512 MB of size.

    For the call to be successful an AWS account is required,
    similar to the
    [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-configure.html)
    requirements.

    Besides the AWS configuration, it is very similar to the other PDF
    loaders, while also supporting JPEG, PNG and TIFF and non-native
    PDF formats.

    ```python
    from langchain_community.document_loaders import AmazonTextractPDFLoader
    loader=AmazonTextractPDFLoader("example_data/alejandro_rosalez_sample-small.jpeg")
    documents = loader.load()
    ```

    One feature is the linearization of the output.
    When using the features LAYOUT, FORMS or TABLES together with Textract

    ```python
    from langchain_community.document_loaders import AmazonTextractPDFLoader
    # you can mix and match each of the features
    loader=AmazonTextractPDFLoader(
        "example_data/alejandro_rosalez_sample-small.jpeg",
        textract_features=["TABLES", "LAYOUT"])
    documents = loader.load()
    ```

    it will generate output that formats the text in reading order and
    try to output the information in a tabular structure or
    output the key/value pairs with a colon (key: value).
    This helps most LLMs to achieve better accuracy when
    processing these texts.

    ``Document`` objects are returned with metadata that includes the ``source`` and
    a 1-based index of the page number in ``page``. Note that ``page`` represents
    the index of the result returned from Textract, not necessarily the as-written
    page number in the document.

    """

    def __init__(
        self,
        textract_features: Optional[Sequence[int]] = None,
        client: Optional[Any] = None,
        *,
        linearization_config: Optional[TextLinearizationConfig] = None,
    ) -> None:
        """Initializes the parser.

        Args:
            textract_features: Features to be used for extraction, each feature
                               should be passed as an int that conforms to the enum
                               `Textract_Features`, see `amazon-textract-caller` pkg
            client: boto3 textract client
            linearization_config: Config to be used for linearization of the output
                                  should be an instance of TextLinearizationConfig from
                                  the `textractor` pkg
        """

        try:
            import textractcaller as tc
            import textractor.entities.document as textractor

            self.tc = tc
            self.textractor = textractor

            if textract_features is not None:
                self.textract_features = [
                    tc.Textract_Features(f) for f in textract_features
                ]
            else:
                self.textract_features = []

            if linearization_config is not None:
                self.linearization_config = linearization_config
            else:
                self.linearization_config = self.textractor.TextLinearizationConfig(
                    hide_figure_layout=True,
                    title_prefix="# ",
                    section_header_prefix="## ",
                    list_element_prefix="*",
                )
        except ImportError:
            raise ImportError(
                "Could not import amazon-textract-caller or "
                "amazon-textract-textractor python package. Please install it "
                "with `pip install amazon-textract-caller` & "
                "`pip install amazon-textract-textractor`."
            )

        if not client:
            try:
                import boto3

                self.boto3_textract_client = boto3.client("textract")
            except ImportError:
                raise ImportError(
                    "Could not import boto3 python package. "
                    "Please install it with `pip install boto3`."
                )
        else:
            self.boto3_textract_client = client

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        """Iterates over the Blob pages and returns an Iterator with a Document
        for each page, like the other parsers If multi-page document, blob.path
        has to be set to the S3 URI and for single page docs
        the blob.data is taken
        """

        url_parse_result = urlparse(str(blob.path)) if blob.path else None
        # Either call with S3 path (multi-page) or with bytes (single-page)
        if (
            url_parse_result
            and url_parse_result.scheme == "s3"
            and url_parse_result.netloc
        ):
            textract_response_json = self.tc.call_textract(
                input_document=str(blob.path),
                features=self.textract_features,
                boto3_textract_client=self.boto3_textract_client,
            )
        else:
            textract_response_json = self.tc.call_textract(
                input_document=blob.as_bytes(),
                features=self.textract_features,
                call_mode=self.tc.Textract_Call_Mode.FORCE_SYNC,
                boto3_textract_client=self.boto3_textract_client,
            )

        document = self.textractor.Document.open(textract_response_json)

        for idx, page in enumerate(document.pages):
            yield Document(
                page_content=page.get_text(config=self.linearization_config),
                metadata={"source": blob.source, "page": idx + 1},
            )


class DocumentIntelligenceParser(BaseBlobParser):
    """Loads a PDF with Azure Document Intelligence
    (formerly Form Recognizer) and chunks at character level."""

    def __init__(self, client: Any, model: str):
        warnings.warn(
            "langchain_community.document_loaders.parsers.pdf.DocumentIntelligenceParser"
            "and langchain_community.document_loaders.pdf.DocumentIntelligenceLoader"
            " are deprecated. Please upgrade to "
            "langchain_community.document_loaders.DocumentIntelligenceLoader "
            "for any file parsing purpose using Azure Document Intelligence "
            "service."
        )
        self.client = client
        self.model = model

    def _generate_docs(self, blob: Blob, result: Any) -> Iterator[Document]:
        for p in result.pages:
            content = " ".join([line.content for line in p.lines])

            d = Document(
                page_content=content,
                metadata={
                    "source": blob.source,
                    "page": p.page_number,
                },
            )
            yield d

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        """Lazily parse the blob."""

        with blob.as_bytes_io() as file_obj:
            poller = self.client.begin_analyze_document(self.model, file_obj)
            result = poller.result()

            docs = self._generate_docs(blob, result)

            yield from docs
