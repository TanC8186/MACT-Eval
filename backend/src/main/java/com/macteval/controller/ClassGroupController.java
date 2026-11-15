package com.macteval.controller;

import com.macteval.dto.*;
import com.macteval.service.ClassGroupService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/classes")
@RequiredArgsConstructor
public class ClassGroupController {

    private final ClassGroupService classGroupService;

    @GetMapping
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<List<ClassGroupDTO>>> listClasses() {
        return ApiResponse.ok(classGroupService.listClassGroups());
    }

    @GetMapping("/{id}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<ClassGroupDTO>> getClass(@PathVariable String id) {
        return ApiResponse.ok(classGroupService.getClassGroup(id));
    }

    @PostMapping
    @PreAuthorize("hasRole('admin')")
    public ResponseEntity<ApiResponse<ClassGroupDTO>> createClass(@RequestBody Map<String, String> body) {
        return ApiResponse.ok(classGroupService.createClassGroup(
                body.get("name"), body.get("grade"), body.get("department")));
    }

    @PutMapping("/{id}")
    @PreAuthorize("hasRole('admin')")
    public ResponseEntity<ApiResponse<ClassGroupDTO>> updateClass(
            @PathVariable String id, @Valid @RequestBody ClassGroupUpdateRequest body) {
        return ApiResponse.ok(classGroupService.updateClassGroup(
                id, body.name(), body.grade(), body.department()));
    }

    @DeleteMapping("/{id}")
    @PreAuthorize("hasRole('admin')")
    public ResponseEntity<ApiResponse<String>> deleteClass(@PathVariable String id) {
        classGroupService.deleteClassGroup(id);
        return ApiResponse.ok("删除成功");
    }
}
