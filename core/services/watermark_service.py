"""Watermark service for applying watermarks to processed images."""
import concurrent.futures
import gc
import os
import threading
from typing import Any, Callable, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageStat

from core.services.global_logger import logFunc


# Type aliases
Block = Tuple[int, int, int, bool]  # (x, y, height, is_white)
Position = Tuple[int, int]  # (x, y)
_WM_DEBUG_ENABLED = os.getenv("SMARTSTITCH_WM_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
_WM_WORKERS_LIMIT = 32
_WM_WORKERS_DEFAULT = min(
    _WM_WORKERS_LIMIT,
    max(4, int(os.getenv("SMARTSTITCH_WATERMARK_WORKERS", str((os.cpu_count() or 8) * 2)))),
)
_WM_FAST_SAVE = os.getenv("SMARTSTITCH_WM_FAST_SAVE", "1").strip().lower() in {"1", "true", "yes", "on"}
_WM_JPEG_SUBSAMPLING = int(os.getenv("SMARTSTITCH_WM_JPEG_SUBSAMPLING", "2" if _WM_FAST_SAVE else "0"))
_WM_WEBP_METHOD = int(os.getenv("SMARTSTITCH_WM_WEBP_METHOD", "0" if _WM_FAST_SAVE else "4"))
_WM_PNG_COMPRESS_LEVEL = int(os.getenv("SMARTSTITCH_WM_PNG_COMPRESS_LEVEL", "0"))
_WM_FULLPAGE_FAST_SELECT = os.getenv("SMARTSTITCH_WM_FULLPAGE_FAST_SELECT", "1").strip().lower() in {"1", "true", "yes", "on"}
_WM_FULLPAGE_INSERT_DEFAULT = os.getenv(
    "SMARTSTITCH_WM_FULLPAGE_INSERT",
    "0" if _WM_FAST_SAVE else "1",
).strip().lower() in {"1", "true", "yes", "on"}
_RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _safe_close(*images) -> None:
    """Safely close PIL images."""
    for img in images:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


class WatermarkService:
    """Service for applying watermarks to processed images.
    
    Supports two types of watermarks:
    - Fullpage: Large watermarks placed in uniform color blocks
    - Overlay: Smaller watermarks with transparency placed anywhere
    
    Also supports header/footer images.
    """
    
    def __init__(self) -> None:
        self._watermarks_fullpage: List[Image.Image] = []
        self._watermarks_overlay: List[Image.Image] = []
        self._index_fullpage: int = 0
        self._index_overlay: int = 0
        self._index_lock = threading.Lock()
        # Cache de watermarks redimensionados keyed por (id(wm), target_w, target_h)
        # Evita resize LANCZOS repetido para cada imagem do capítulo
        self._resize_cache: dict[tuple, Image.Image] = {}
        self._resize_lock = threading.Lock()
        self._last_run_info: dict[str, int | bool] = {
            "requested_workers": 0,
            "used_workers": 0,
            "parallel": False,
            "total_images": 0,
        }

    @property
    def last_run_info(self) -> dict[str, int | bool]:
        return dict(self._last_run_info)

    def _dbg(self, message: str) -> None:
        """Verbose debug logger for watermark diagnostics."""
        if _WM_DEBUG_ENABLED:
            print(f"[WM-DEBUG] {message}")
    
    @property
    def watermarks_fullpage(self) -> List[Image.Image]:
        return self._watermarks_fullpage
    
    @property
    def watermarks_overlay(self) -> List[Image.Image]:
        return self._watermarks_overlay

    def load_watermarks(
        self, 
        fullpage_paths: List[str], 
        overlay_paths: List[str]
    ) -> bool:
        """Load watermark images from paths.
        
        Args:
            fullpage_paths: List of paths to fullpage watermark images
            overlay_paths: List of paths to overlay watermark images
            
        Returns:
            True if at least one watermark was loaded
        """
        self._dbg(
            f"load_watermarks start: fullpage_paths={len(fullpage_paths)}, "
            f"overlay_paths={len(overlay_paths)}"
        )
        self.close_watermarks()
        
        for path in fullpage_paths:
            self._dbg(f"load fullpage watermark path='{path}'")
            if path and os.path.isfile(path):
                try:
                    wm = Image.open(path).convert("RGBA")
                    self._watermarks_fullpage.append(wm)
                    self._dbg(
                        f"loaded fullpage watermark: path='{path}', size={wm.size}, mode={wm.mode}"
                    )
                except (OSError, IOError) as e:
                    print(f"Warning: Could not load fullpage watermark '{path}': {e}")
            else:
                self._dbg(f"fullpage watermark path skipped (invalid/missing): '{path}'")
        
        for path in overlay_paths:
            self._dbg(f"load overlay watermark path='{path}'")
            if path and os.path.isfile(path):
                try:
                    wm = Image.open(path).convert("RGBA")
                    self._watermarks_overlay.append(wm)
                    self._dbg(
                        f"loaded overlay watermark: path='{path}', size={wm.size}, mode={wm.mode}"
                    )
                except (OSError, IOError) as e:
                    print(f"Warning: Could not load overlay watermark '{path}': {e}")
            else:
                self._dbg(f"overlay watermark path skipped (invalid/missing): '{path}'")

        loaded_any = bool(self._watermarks_fullpage or self._watermarks_overlay)
        self._dbg(
            f"load_watermarks done: fullpage_loaded={len(self._watermarks_fullpage)}, "
            f"overlay_loaded={len(self._watermarks_overlay)}, loaded_any={loaded_any}"
        )
        return loaded_any
    
    def close_watermarks(self) -> None:
        """Close all loaded watermark images to free memory."""
        self._dbg(
            f"close_watermarks: closing fullpage={len(self._watermarks_fullpage)}, "
            f"overlay={len(self._watermarks_overlay)}"
        )
        for wm in self._watermarks_fullpage + self._watermarks_overlay:
            _safe_close(wm)
        self._watermarks_fullpage = []
        self._watermarks_overlay = []
        self._index_fullpage = 0
        self._index_overlay = 0
        with self._resize_lock:
            for _cached in self._resize_cache.values():
                _safe_close(_cached)
            self._resize_cache.clear()

    def _get_resized_wm(self, watermark: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """Retorna watermark redimensionado, usando cache para evitar Lanczos repetido."""
        key = (id(watermark), target_w, target_h)
        cached = self._resize_cache.get(key)
        if cached is not None:
            return cached
        with self._resize_lock:
            if key not in self._resize_cache:
                self._resize_cache[key] = watermark.resize((target_w, target_h), _RESAMPLE_LANCZOS)
            return self._resize_cache[key]

    def get_next_watermark_fullpage(self) -> Optional[Image.Image]:
        """Get next fullpage watermark in cyclic order."""
        with self._index_lock:
            if not self._watermarks_fullpage:
                self._dbg("get_next_watermark_fullpage: no watermark loaded")
                return None
            wm = self._watermarks_fullpage[self._index_fullpage]
            self._dbg(
                f"get_next_watermark_fullpage: index={self._index_fullpage}, size={wm.size}"
            )
            self._index_fullpage = (self._index_fullpage + 1) % len(self._watermarks_fullpage)
            return wm
    
    def get_next_watermark_overlay(self) -> Optional[Image.Image]:
        """Get next overlay watermark in cyclic order."""
        with self._index_lock:
            if not self._watermarks_overlay:
                return None
            wm = self._watermarks_overlay[self._index_overlay]
            self._index_overlay = (self._index_overlay + 1) % len(self._watermarks_overlay)
            return wm
    
    def find_uniform_blocks_fullpage(
        self, 
        image: Image.Image, 
        watermark: Image.Image, 
        threshold: int = 200
    ) -> List[Block]:
        """Encontra todos os blocos uniformes adequados para marcas d'água de página inteira.
        
        Procura faixas horizontais TOTALMENTE uniformes (brancas ou pretas).
        MUITO RIGOROSO:
        - 100% dos pixels devem ser branco puro (255) ou preto puro (0)
        - Rejeita qualquer área com conteúdo visível (balões, texto, cores)
        """
        width, height = image.size
        wm_width, wm_height = watermark.size
        self._dbg(
            f"find_uniform_blocks_fullpage start: image_size={image.size}, wm_size={watermark.size}, "
            f"threshold={threshold}"
        )
        grayscale = image.convert("L")
        img_array = np.array(grayscale, dtype=np.uint8)
        grayscale.close()

        # Vetorizado: min/max por linha em 2 operações numpy vs ~H/step chamadas no loop original
        row_min = img_array.min(axis=1)   # shape (height,)
        row_max = img_array.max(axis=1)   # shape (height,)
        is_white_row = (row_min == 255) & (row_max == 255)
        is_black_row = (row_min == 0) & (row_max == 0)
        is_uniform = is_white_row | is_black_row

        # Encontra runs contíguas usando np.diff (sem loop Python por linha)
        padded = np.zeros(len(is_uniform) + 2, dtype=np.int8)
        padded[1:-1] = is_uniform.astype(np.int8)
        changes = np.diff(padded)
        run_starts = np.where(changes == 1)[0]
        run_ends = np.where(changes == -1)[0]

        blocks = []
        for start, end in zip(run_starts, run_ends):
            block_h = int(end - start)
            if block_h >= wm_height:
                color_is_white = bool(is_white_row[start])
                blocks.append((0, int(start), block_h, color_is_white))
                self._dbg(
                    f"accepted block: y={int(start)}, height={block_h}, "
                    f"type={'white' if color_is_white else 'black'}"
                )

        self._dbg(f"find_uniform_blocks_fullpage done: total_blocks={len(blocks)}")
        return blocks
    
    def calculate_watermark_position_in_block(
        self, 
        block_x: int, 
        block_y: int, 
        block_height: int,
        wm_width: int, 
        wm_height: int, 
        position_type: int,
        image_width: int,
        spacing_top: int = 50,
        spacing_bottom: int = 50,
        spacing_sides: int = 10
    ) -> Tuple[int, int]:
        """Calculate watermark position within block with configurable spacing.
        
        Args:
            block_x: Block X position
            block_y: Block Y position
            block_height: Block height
            wm_width: Watermark width
            wm_height: Watermark height
            position_type: Position type (TOP, CENTER, BOTTOM)
            image_width: Total image width for horizontal centering
            spacing_top: Minimum spacing from top (default 50px)
            spacing_bottom: Minimum spacing from bottom (default 50px)
            spacing_sides: Minimum spacing from sides (default 10px)
            
        Returns:
            Tuple of (x, y) position for watermark
        """
        from core.utils.constants import WATERMARK_FULLPAGE_POSITION
        
        # Vertical position based on position_type
        if position_type == WATERMARK_FULLPAGE_POSITION.TOP:
            wm_y = block_y + spacing_top
        elif position_type == WATERMARK_FULLPAGE_POSITION.BOTTOM:
            wm_y = block_y + block_height - wm_height - spacing_bottom
        else:  # CENTER
            wm_y = block_y + (block_height - wm_height) // 2
        
        # Horizontal position - center in the image
        wm_x = (image_width - wm_width) // 2
        
        return wm_x, wm_y
    
    def _validate_block_has_space(
        self,
        block_height: int,
        wm_height: int,
        position_type: int,
        spacing_top: int,
        spacing_bottom: int,
        require_centered: bool
    ) -> bool:
        """Validate if block has enough space for watermark with spacing.
        
        Args:
            block_height: Height of the uniform block
            wm_height: Height of the watermark
            position_type: Position type (TOP, CENTER, BOTTOM)
            spacing_top: Required spacing from top
            spacing_bottom: Required spacing from bottom
            require_centered: If True, requires space above AND below watermark
            
        Returns:
            True if block has sufficient space
        """
        from core.utils.constants import WATERMARK_FULLPAGE_POSITION
        
        if position_type == WATERMARK_FULLPAGE_POSITION.TOP:
            # Need: spacing_top + wm_height + (spacing_bottom if centered required)
            required = spacing_top + wm_height
            if require_centered:
                required += spacing_bottom
            return block_height >= required
            
        elif position_type == WATERMARK_FULLPAGE_POSITION.BOTTOM:
            # Need: (spacing_top if centered required) + wm_height + spacing_bottom
            required = wm_height + spacing_bottom
            if require_centered:
                required += spacing_top
            return block_height >= required
            
        else:  # CENTER
            # Need: spacing_top + wm_height + spacing_bottom
            required = spacing_top + wm_height + spacing_bottom
            return block_height >= required

    @logFunc(inclass=True)
    def find_suitable_space_overlay(self, image: Image.Image, watermark: Image.Image, 
                               threshold_white: int = 250, threshold_black: int = 30, 
                               contrast_threshold: int = 50) -> Optional[Tuple[int, int]]:
        """Encontra espaço adequado para marca d'água de sobreposição com detecção otimizada."""
        width, height = image.size
        wm_width, wm_height = watermark.size
        if wm_width <= 0 or wm_height <= 0:
            return None
        if wm_width > width or wm_height > height:
            return (0, 0)

        grayscale = image.convert("L")

        # Análise estatística global para adaptar parâmetros
        stat_full = ImageStat.Stat(grayscale)
        full_mean = stat_full.mean[0]
        full_stddev = stat_full.stddev[0]
        
        # Se a imagem toda tem baixo contraste (uniforme), aplica em posição padrão
        if full_stddev < 5:
            grayscale.close()
            return (max(0, min(50, width - wm_width)), max(0, min(50, height - wm_height)))
        
        # Adapta thresholds baseado nas características da imagem
        adapted_white = threshold_white
        adapted_black = threshold_black
        adapted_contrast = contrast_threshold
        
        if full_mean > 200:  # Imagem clara
            adapted_white = min(threshold_white + 10, 255)
            adapted_contrast = contrast_threshold + 10
        elif full_mean < 100:  # Imagem escura
            adapted_black = max(threshold_black - 10, 10)
            adapted_contrast = contrast_threshold - 10

        # Busca otimizada por grid
        step_size = max(8, min(wm_width // 4, wm_height // 2))
        
        # Guarda apenas o melhor candidato para reduzir custo de memoria/ordenacao
        best_score = -1
        best_position: Optional[Tuple[int, int]] = None
        center_x = width // 2
        center_y = height // 2
        max_dist = ((width // 2) ** 2 + (height // 2) ** 2) ** 0.5 or 1.0
        
        for y in range(0, height - wm_height, step_size):
            for x in range(0, width - wm_width, step_size):
                box = (x, y, x + wm_width, y + wm_height)
                region = grayscale.crop(box)
                stat = ImageStat.Stat(region)

                mean = stat.mean[0]
                stddev = stat.stddev[0]

                # Calcula score de adequação (maior = melhor)
                score = 0
                
                # Evitar extremos de brightness
                if adapted_black < mean < adapted_white:
                    score += 50
                
                # Preferir áreas de médio contraste (não texto, não uniforme demais)
                if 15 < stddev < adapted_contrast:
                    score += 30
                elif stddev <= 15:  # Área uniforme, ok mas não ideal
                    score += 20
                
                # Bonus para posições mais centrais
                dist_from_center = ((x - center_x) ** 2 + (y - center_y) ** 2) ** 0.5
                center_bonus = int(20 * (1 - dist_from_center / max_dist))
                score += center_bonus
                
                if score > 40:  # Threshold mínimo de qualidade
                    if score > best_score:
                        best_score = score
                        best_position = (x, y)

                region.close()
        
        # Retorna a melhor posição
        if best_position is not None:
            grayscale.close()
            return best_position

        # Fallback: posição padrão se nada foi encontrado
        grayscale.close()
        return (50, 50)

    @logFunc(inclass=True)
    def add_watermark_fullpage(
        self,
        image: Image.Image,
        settings: dict[str, Any] | None = None,
    ) -> Optional[Image.Image]:
        """Adiciona marca d'água de página inteira com configurações de posicionamento e frequência."""
        self._dbg("add_watermark_fullpage start")
        if not self.watermarks_fullpage:
            self._dbg("add_watermark_fullpage aborted: no fullpage watermarks loaded")
            return None
        
        # Configurações padrão se não fornecidas
        if settings is None:
            settings = {}
        
        from core.utils.constants import WATERMARK_FULLPAGE_POSITION
        
        # Fullpage watermark settings
        position_type = WATERMARK_FULLPAGE_POSITION.CENTER
        threshold = settings.get('watermark_fullpage_threshold', 200)
        max_per_page = settings.get('watermark_fullpage_max_per_page', 1)
        
        # Spacing settings
        spacing_top = settings.get('watermark_fullpage_min_spacing_top', 50)
        spacing_bottom = settings.get('watermark_fullpage_min_spacing_bottom', 50)
        spacing_sides = settings.get('watermark_fullpage_min_spacing_sides', 10)
        require_centered = settings.get('watermark_fullpage_require_centered_space', True)
        
        # Insert mode settings
        insert_mode = settings.get('watermark_fullpage_insert_mode', _WM_FULLPAGE_INSERT_DEFAULT)
        min_area_height = settings.get('watermark_fullpage_min_area_height', 400)
        self._dbg(
            "add_watermark_fullpage settings: "
            f"position_type={position_type}, threshold={threshold}, max_per_page={max_per_page}, "
            f"spacing_top={spacing_top}, spacing_bottom={spacing_bottom}, spacing_sides={spacing_sides}, "
            f"require_centered={require_centered}, insert_mode={insert_mode}, "
            f"min_area_height={min_area_height}"
        )
        
        watermark = self.get_next_watermark_fullpage()
        if watermark is None:
            self._dbg("add_watermark_fullpage aborted: get_next_watermark_fullpage returned None")
            return None
        
        # Redimensiona para largura total da imagem (ponta a ponta)
        wm_width = image.width
        if wm_width <= 0:
            self._dbg(f"add_watermark_fullpage aborted: invalid wm_width={wm_width}")
            return None
            
        aspect_ratio = watermark.height / watermark.width
        wm_height = int(wm_width * aspect_ratio)
        
        # Garante que a altura não ultrapasse a altura da imagem
        if wm_height > image.height - 3:
            wm_height = image.height - 3
            wm_width = int(wm_height / aspect_ratio)
            
        if wm_width <= 0 or wm_height <= 0:
            self._dbg(
                f"add_watermark_fullpage aborted: invalid resized dimensions "
                f"wm_width={wm_width}, wm_height={wm_height}"
            )
            return None
            
        resized_watermark = self._get_resized_wm(watermark, wm_width, wm_height)
        self._dbg(f"fullpage watermark resized to {resized_watermark.size}")
        
        # Encontra todos os blocos uniformes
        blocks = self.find_uniform_blocks_fullpage(image, resized_watermark, threshold)
        self._dbg(f"blocks detected={len(blocks)}")
        
        if not blocks:
            self._dbg("add_watermark_fullpage aborted: no uniform blocks found")
            return None
        
        selected_blocks = []
        if max_per_page is None:
            max_per_page = 1

        try:
            max_per_page_int = int(max_per_page)
        except (TypeError, ValueError):
            max_per_page_int = 1

        if max_per_page_int <= 0:
            self._dbg(f"add_watermark_fullpage aborted: max_per_page_int={max_per_page_int}")
            return None

        # blocks format: (x, y, block_height, is_white_block)
        # Filter blocks that have sufficient space for watermark with spacing.
        # Fast path avoids expensive per-block crop/stat analysis.
        valid_blocks: List[Block] = []
        for block in blocks:
            block_x, block_y, block_height, is_white = block
            self._dbg(
                f"evaluate block: x={block_x}, y={block_y}, height={block_height}, "
                f"is_white={is_white}"
            )
            
            # Check minimum area height requirement
            if block_height < min_area_height:
                self._dbg(
                    f"block rejected: height {block_height} < min_area_height {min_area_height}"
                )
                continue
            
            # Validate block has enough space with spacing requirements
            if not self._validate_block_has_space(
                block_height, wm_height, position_type,
                spacing_top, spacing_bottom, require_centered
            ):
                self._dbg(
                    "block rejected: insufficient spacing/space for watermark "
                    f"(block_height={block_height}, wm_height={wm_height}, "
                    f"spacing_top={spacing_top}, spacing_bottom={spacing_bottom}, "
                    f"require_centered={require_centered})"
                )
                continue

            valid_blocks.append(block)

        if _WM_FULLPAGE_FAST_SELECT:
            valid_blocks.sort(key=lambda b: b[2], reverse=True)
            selected_blocks = valid_blocks[:max_per_page_int]
        else:
            selected_blocks = valid_blocks[:max_per_page_int]
        self._dbg(
            f"candidate_count={len(valid_blocks)}, selected_blocks={len(selected_blocks)}, "
            f"max_per_page_int={max_per_page_int}"
        )
        
        if not selected_blocks:
            self._dbg("add_watermark_fullpage aborted: no selected blocks after filtering")
            return None
        
        # Insert mode: cut and insert watermark in the middle of uniform area
        if insert_mode:
            result = image.convert("RGBA")
            inserted_any = False
            self._dbg("insert_mode enabled: starting insertion loop")
            
            # Process blocks in reverse order (bottom to top) to maintain correct positions
            for block_x, block_y, block_height, is_white_block in reversed(selected_blocks):
                # Respect top/bottom spacing inside the uniform block before inserting.
                # This avoids cuts too close to artwork boundaries.
                safe_top = block_y + spacing_top
                safe_bottom = block_y + block_height - spacing_bottom
                if safe_bottom <= safe_top:
                    self._dbg(
                        f"skip insertion block due to invalid safe range: "
                        f"block_y={block_y}, block_height={block_height}, "
                        f"safe_top={safe_top}, safe_bottom={safe_bottom}"
                    )
                    continue
                insert_y = safe_top + ((safe_bottom - safe_top) // 2)
                self._dbg(
                    f"insert block: x={block_x}, y={block_y}, height={block_height}, "
                    f"safe_top={safe_top}, safe_bottom={safe_bottom}, insert_y={insert_y}"
                )
                
                # Split image at insertion point
                top_part = result.crop((0, 0, result.width, insert_y))
                bottom_part = result.crop((0, insert_y, result.width, result.height))
                
                # Create new image with increased height
                new_height = result.height + wm_height
                new_image = Image.new("RGBA", (result.width, new_height), (255, 255, 255, 0))
                
                # Paste top part
                new_image.paste(top_part, (0, 0))
                
                # Paste watermark at x=0 (full width, edge to edge)
                new_image.paste(resized_watermark, (0, insert_y), resized_watermark)
                
                # Paste bottom part after watermark
                new_image.paste(bottom_part, (0, insert_y + wm_height))
                
                # Clean up
                top_part.close()
                bottom_part.close()
                result.close()
                
                result = new_image
                inserted_any = True
                self._dbg(
                    f"watermark inserted at y={insert_y}; new_result_size={result.size}"
                )
            
            if not inserted_any:
                self._dbg("insert_mode finished with no insertions")
                _safe_close(result)
                return None
            self._dbg(f"insert_mode done: final_size={result.size}")
            return result
        
        # Overlay mode: paste watermark on top of image (original behavior)
        else:
            watermark_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
            self._dbg("insert_mode disabled: using overlay-on-blocks mode")
            
            # Aplica marca d'água nos blocos selecionados com espaçamento configurável
            for block_x, block_y, block_height, is_white_block in selected_blocks:
                wm_x, wm_y = self.calculate_watermark_position_in_block(
                    block_x, block_y, block_height, wm_width, wm_height, position_type,
                    image.width, spacing_top, spacing_bottom, spacing_sides
                )
                
                # Garante que a posição está dentro dos limites da imagem
                wm_x = max(0, min(wm_x, image.width - wm_width))
                wm_y = max(0, min(wm_y, image.height - wm_height))
                
                watermark_layer.paste(resized_watermark, (wm_x, wm_y), resized_watermark)
                self._dbg(
                    f"overlay block paste: wm_x={wm_x}, wm_y={wm_y}, wm_size={resized_watermark.size}"
                )
            
            # Combina com a imagem original
            result = Image.alpha_composite(image.convert("RGBA"), watermark_layer)
            
            watermark_layer.close()
            self._dbg(f"overlay-on-blocks mode done: result_size={result.size}")
            
            return result

    @logFunc(inclass=True)
    def add_watermark_overlay(
        self,
        image: Image.Image,
        settings: dict[str, Any] | None = None,
    ) -> Optional[Image.Image]:
        """Adiciona marca d'água de sobreposição - menor, com transparência."""
        if not self.watermarks_overlay:
            return None
            
        return self.add_watermark_overlay_configurable(image, settings)

    @logFunc(inclass=True)
    def add_watermark_overlay_configurable(
        self,
        image: Image.Image,
        settings: dict[str, Any] | None = None,
    ) -> Optional[Image.Image]:
        if not self.watermarks_overlay:
            return None

        if settings is None:
            settings = {}

        from core.utils.constants import WATERMARK_OVERLAY_POSITION

        position_type = settings.get('watermark_overlay_position', WATERMARK_OVERLAY_POSITION.AUTO)
        opacity = settings.get('watermark_overlay_opacity', 80)
        scale_pct = settings.get('watermark_overlay_scale_pct', 50)
        max_per_page = settings.get('watermark_overlay_max_per_page', 1)
        margin = settings.get('watermark_overlay_margin', 10)
        min_space = settings.get('watermark_overlay_min_space_around', 30)

        try:
            opacity_f = max(0.0, min(100.0, float(opacity))) / 100.0
        except Exception:
            opacity_f = 0.8

        try:
            scale_f = max(0.05, min(1.0, float(scale_pct) / 100.0))
        except Exception:
            scale_f = 0.5

        try:
            max_int = int(max_per_page)
        except Exception:
            max_int = 1

        if max_int <= 0:
            return None
        base = image.convert("RGBA")
        modified = False
        for _ in range(max_int):
            watermark = self.get_next_watermark_overlay()
            if watermark is None:
                break

            wm_width = int(base.width * scale_f)
            if wm_width <= 0:
                continue
            aspect_ratio = watermark.height / watermark.width
            wm_height = int(wm_width * aspect_ratio)
            if wm_height <= 0:
                continue

            resized_watermark = self._get_resized_wm(watermark, wm_width, wm_height)

            position = None
            if position_type == WATERMARK_OVERLAY_POSITION.AUTO:
                position = self.find_suitable_space_overlay(base, resized_watermark)
            else:
                max_x = max(margin, base.width - wm_width - margin)
                max_y = max(margin, base.height - wm_height - margin)

                if position_type == WATERMARK_OVERLAY_POSITION.TOP_LEFT:
                    position = (margin, margin)
                elif position_type == WATERMARK_OVERLAY_POSITION.TOP_RIGHT:
                    position = (max_x, margin)
                elif position_type == WATERMARK_OVERLAY_POSITION.BOTTOM_LEFT:
                    position = (margin, max_y)
                elif position_type == WATERMARK_OVERLAY_POSITION.BOTTOM_RIGHT:
                    position = (max_x, max_y)
                elif position_type == WATERMARK_OVERLAY_POSITION.CENTER:
                    position = ((base.width - wm_width) // 2, (base.height - wm_height) // 2)
                else:
                    position = (margin, margin)

            if not position:
                break

            wm = resized_watermark.copy()
            alpha = wm.split()[3]
            alpha = alpha.point(lambda p: int(p * opacity_f))
            wm.putalpha(alpha)

            watermark_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
            watermark_layer.paste(wm, position, wm)
            merged = Image.alpha_composite(base, watermark_layer)

            base.close()
            base = merged
            modified = True

            wm.close()
            watermark_layer.close()

        if not modified:
            base.close()
            return None

        return base
    
    @logFunc(inclass=True)
    def add_header_footer_images(
        self,
        image_path: str,
        header_images: List[str],
        footer_images: List[str],
        lossy_quality: int = 100,
    ) -> bool:
        """Adiciona imagens de cabeçalho e rodapé."""
        try:
            with Image.open(image_path) as img_original:
                width, height = img_original.size
                total_header_height = 0
                total_footer_height = 0
                
                # Processa imagens de cabeçalho
                header_imgs = []
                for header_path in header_images:
                    if os.path.exists(header_path):
                        with Image.open(header_path) as header_img:
                            if header_img.width != width:
                                new_height = int(header_img.height * (width / header_img.width))
                                header_img = header_img.resize((width, new_height), _RESAMPLE_LANCZOS)
                            else:
                                header_img = header_img.copy()
                            header_imgs.append(header_img)
                            total_header_height += header_img.height
                
                # Processa imagens de rodapé
                footer_imgs = []
                for footer_path in footer_images:
                    if os.path.exists(footer_path):
                        with Image.open(footer_path) as footer_img:
                            if footer_img.width != width:
                                new_height = int(footer_img.height * (width / footer_img.width))
                                footer_img = footer_img.resize((width, new_height), _RESAMPLE_LANCZOS)
                            else:
                                footer_img = footer_img.copy()
                            footer_imgs.append(footer_img)
                            total_footer_height += footer_img.height
                
                # Cria nova imagem com o tamanho total
                new_height = height + total_header_height + total_footer_height
                new_img = Image.new("RGB", (width, new_height))
                
                y_offset = 0
                
                # Cola imagens de cabeçalho
                for header_img in header_imgs:
                    new_img.paste(header_img, (0, y_offset))
                    y_offset += header_img.height
                
                # Cola imagem original
                new_img.paste(img_original, (0, y_offset))
                y_offset += height
                
                # Cola imagens de rodapé
                for footer_img in footer_imgs:
                    new_img.paste(footer_img, (0, y_offset))
                    y_offset += footer_img.height
                
                # Salva a nova imagem com qualidade máxima
                ext = os.path.splitext(image_path)[1].lower()
                if ext in ('.jpg', '.jpeg'):
                    new_img.save(
                        image_path,
                        quality=lossy_quality,
                        subsampling=max(0, min(2, _WM_JPEG_SUBSAMPLING)),
                        optimize=False,
                    )
                elif ext == '.webp':
                    new_img.save(
                        image_path,
                        quality=lossy_quality,
                        method=max(0, min(6, _WM_WEBP_METHOD)),
                    )
                elif ext == '.png':
                    new_img.save(image_path, compress_level=max(0, min(9, _WM_PNG_COMPRESS_LEVEL)))
                else:
                    new_img.save(image_path)
                new_img.close()
                
                # Limpa memória
                for img in header_imgs + footer_imgs:
                    img.close()
                
                return True
                
        except Exception as e:
            print(f"Erro ao adicionar header/footer em {image_path}: {e}")
            return False
    
    def process_chapter_folder(
        self, 
        chapter_path: str, 
        settings: dict, 
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> bool:
        """Process a chapter folder applying watermarks to all images.
        
        Args:
            chapter_path: Path to the chapter folder
            settings: Watermark settings dictionary
            progress_callback: Optional callback(current, total, message)
            
        Returns:
            True if processing was successful
        """
        if not os.path.isdir(chapter_path):
            self._dbg(f"process_chapter_folder aborted: invalid chapter_path='{chapter_path}'")
            return False
        
        images = sorted([
            f for f in os.listdir(chapter_path) 
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ])
        
        if not images:
            self._dbg(f"process_chapter_folder aborted: no images in '{chapter_path}'")
            return False
        
        total_images = len(images)
        fullpage_enabled = settings.get('watermark_fullpage_enabled', False)
        overlay_enabled = settings.get('watermark_overlay_enabled', False)
        requested_workers_raw = settings.get("watermark_max_workers", _WM_WORKERS_DEFAULT)
        try:
            requested_workers = int(requested_workers_raw)
        except (TypeError, ValueError):
            requested_workers = _WM_WORKERS_DEFAULT

        max_workers = max(1, min(requested_workers, _WM_WORKERS_LIMIT, total_images))
        self._last_run_info = {
            "requested_workers": requested_workers,
            "used_workers": max_workers,
            "parallel": max_workers > 1,
            "total_images": total_images,
        }
        self._dbg(
            f"process_chapter_folder start: chapter='{chapter_path}', total_images={total_images}, "
            f"fullpage_enabled={fullpage_enabled}, overlay_enabled={overlay_enabled}, "
            f"add_header={settings.get('add_header', False)}, add_footer={settings.get('add_footer', False)}, "
            f"max_workers={max_workers}"
        )

        def _process_one(i: int, image_name: str) -> tuple[int, str, Optional[Exception]]:
            image_path = os.path.join(chapter_path, image_name)
            try:
                self._dbg(f"processing image {i + 1}/{total_images}: '{image_path}'")
                self._apply_watermarks_to_image(
                    image_path,
                    settings,
                    fullpage_enabled,
                    overlay_enabled,
                )
                return i, image_name, None
            except Exception as e:
                return i, image_name, e

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_process_one, i, image_name): (i, image_name)
                for i, image_name in enumerate(images)
            }
            for future in concurrent.futures.as_completed(futures):
                _, image_name, error = future.result()
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_images, f"Processing {image_name}")
                if error is not None:
                    image_path = os.path.join(chapter_path, image_name)
                    self._dbg(f"processing failed for '{image_path}': {error}")
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    raise RuntimeError(f"Error processing {image_path}: {error}") from error
        
        # Add header to first image
        if settings.get('add_header', False) and settings.get('header_images'):
            first_image_path = os.path.join(chapter_path, images[0])
            self._dbg(f"adding header to '{first_image_path}'")
            lossy_quality = int(settings.get("lossy_quality", 100))
            self.add_header_footer_images(
                first_image_path, 
                settings['header_images'], 
                [],
                lossy_quality,
            )
        
        # Add footer to last image
        if settings.get('add_footer', False) and settings.get('footer_images'):
            last_image_path = os.path.join(chapter_path, images[-1])
            self._dbg(f"adding footer to '{last_image_path}'")
            lossy_quality = int(settings.get("lossy_quality", 100))
            self.add_header_footer_images(
                last_image_path, 
                [], 
                settings['footer_images'],
                lossy_quality,
            )
        
        gc.collect()
        self._dbg(f"process_chapter_folder done: chapter='{chapter_path}'")
        return True
    
    def _apply_watermarks_to_image(
        self,
        image_path: str,
        settings: dict,
        fullpage_enabled: bool,
        overlay_enabled: bool
    ) -> None:
        """Apply watermarks to a single image file.
        
        Args:
            image_path: Path to the image file
            settings: Watermark settings dictionary
            fullpage_enabled: Whether fullpage watermarks are enabled
            overlay_enabled: Whether overlay watermarks are enabled
        """
        self._dbg(
            f"_apply_watermarks_to_image start: image_path='{image_path}', "
            f"fullpage_enabled={fullpage_enabled}, overlay_enabled={overlay_enabled}, "
            f"loaded_fullpage={len(self._watermarks_fullpage)}, loaded_overlay={len(self._watermarks_overlay)}"
        )

        should_apply_fullpage = fullpage_enabled and bool(self._watermarks_fullpage)
        should_apply_overlay = overlay_enabled and bool(self._watermarks_overlay)
        if not should_apply_fullpage and not should_apply_overlay:
            self._dbg("_apply_watermarks_to_image skipped: no loaded/active visual watermarks")
            return

        with Image.open(image_path) as img:
            result = img.convert("RGBA")
            modified = False
            
            # Apply fullpage watermark if enabled
            if should_apply_fullpage:
                fp_result = self.add_watermark_fullpage(result, settings)
                if fp_result:
                    _safe_close(result)
                    result = fp_result
                    modified = True
                    self._dbg(f"fullpage watermark applied: image_path='{image_path}', result_size={result.size}")
                else:
                    self._dbg(f"fullpage watermark NOT applied: image_path='{image_path}'")
            else:
                self._dbg(
                    f"fullpage watermark skipped: enabled={fullpage_enabled}, "
                    f"loaded={len(self._watermarks_fullpage)}"
                )
            
            # Apply overlay watermark if enabled
            if should_apply_overlay:
                ov_result = self.add_watermark_overlay(result, settings)
                if ov_result:
                    _safe_close(result)
                    result = ov_result
                    modified = True
                    self._dbg(f"overlay watermark applied: image_path='{image_path}', result_size={result.size}")
                else:
                    self._dbg(f"overlay watermark NOT applied: image_path='{image_path}'")
            else:
                self._dbg(
                    f"overlay watermark skipped: enabled={overlay_enabled}, "
                    f"loaded={len(self._watermarks_overlay)}"
                )
            
            if modified:
                # Convert RGBA to RGB with white background and save with max quality
                rgb_result = Image.new("RGB", result.size, (255, 255, 255))
                rgb_result.paste(result, mask=result.split()[-1])
                # Determine format and save with appropriate quality
                ext = os.path.splitext(image_path)[1].lower()
                lossy_quality = int(settings.get("lossy_quality", 100))
                if ext in ('.jpg', '.jpeg'):
                    rgb_result.save(
                        image_path,
                        quality=lossy_quality,
                        subsampling=max(0, min(2, _WM_JPEG_SUBSAMPLING)),
                        optimize=False,
                    )
                elif ext == '.webp':
                    rgb_result.save(
                        image_path,
                        quality=lossy_quality,
                        method=max(0, min(6, _WM_WEBP_METHOD)),
                    )
                elif ext == '.png':
                    rgb_result.save(image_path, compress_level=max(0, min(9, _WM_PNG_COMPRESS_LEVEL)))
                else:
                    rgb_result.save(image_path)
                _safe_close(rgb_result)
                self._dbg(f"saved modified image: path='{image_path}', ext='{ext}', size={result.size}")
            else:
                self._dbg(f"image unchanged (no watermark applied): path='{image_path}'")
            
            _safe_close(result)
            self._dbg(f"_apply_watermarks_to_image done: image_path='{image_path}'")
